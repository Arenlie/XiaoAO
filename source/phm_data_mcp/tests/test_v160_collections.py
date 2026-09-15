import re
from types import SimpleNamespace
import pytest
from app.alarm.collection import query_collection as alarms
from app.services.health_collection import query_collection as health
from app.services.readonly_snapshot import query, review
from app.alarm.sql_validator import validate_read_only_sql, validate_required_filters
from app.models import AlarmQuerySpec
from app.alarm.constants import TABLE_BY_TYPE
from datetime import datetime


def test_health_ranks_all_inputs_keeps_ties_missing_and_data_time():
    class Service:
        def query(self,kind,code):
            if code=="bad":raise TimeoutError()
            return {"success":True,"data":{"score":50 if code!="offline" else 0,"grade":5 if code=="offline" else 1,"ts":1789000000000}}
    result=health(Service(),"device",["E3","E2","E1","offline","bad"],limit=2)
    assert [r["scope_id"] for r in result["items"]]==["E1","E2"]
    assert result["boundary_tie_count"]==3 and result["omitted_ties"]==1
    assert result["missing_count"]==2 and not result["complete"]
    assert all(x["health_time"] for x in result["items"])


def test_alarm_distinct_across_types_zeros_and_bound_parameters():
    calls=[]
    class Repo:
        def query(self,sql,params):
            calls.append((sql,params))
            return [{"equip_no":"E1","metric_value":3}]
    result=alarms(Repo(),["E1","E2"],metric="distinct_equipment")
    assert result["total"]==1 and result["items"][1]["alarm_count"]==0
    assert len(calls)==4 and all("%s" in sql and "E1" not in sql for sql,p in calls)


def test_alarm_failure_never_becomes_zero():
    with pytest.raises(TimeoutError):alarms(SimpleNamespace(query=lambda *a: (_ for _ in ()).throw(TimeoutError())),["E1"])


def test_alarm_compare_requires_valid_periods_and_all_state():
    with pytest.raises(ValueError):alarms(None,["E1"],operation="compare")
    with pytest.raises(ValueError):alarms(None,["E1"],start_time="2026-09-10",end_time="2026-09-09")


def test_isolated_sql_executes_median_and_group_on_all_rows():
    result=query([{"area":"甲","health_score":x} for x in [10,20,90]],
        'SELECT area AS "区域", MEDIAN(health_score) AS "中位数" FROM input_rows GROUP BY area',complete=True)
    assert result["items"]==[{"区域":"甲","中位数":20}]
    assert result["review"]["production_sql_executed"] is False


@pytest.mark.parametrize("sql",[
    "DROP TABLE input_rows", "SELECT * FROM input_rows; SELECT 1",
    "SELECT * FROM secret", "SELECT load_extension('/tmp/x') FROM input_rows",
    "SELECT randomblob(10000000) FROM input_rows", "SELECT * FROM input_rows a JOIN input_rows b",
    "WITH RECURSIVE x AS (SELECT 1) SELECT * FROM x", "SELECT * FROM input_rows WHERE health_score>50",
    "SELECT health_score FROM input_rows -- bypass", "SELECT password FROM input_rows",
    "SELECT * FROM input_rows LIMIT -1 OFFSET 1",
])
def test_snapshot_rejects_unsafe_or_scope_changing_sql(sql):
    with pytest.raises(Exception):review(sql,{"health_score"})


def test_sql_partial_data_cannot_fallback():
    with pytest.raises(ValueError):query([{"health_score":12}],"SELECT AVG(health_score) FROM input_rows",complete=False)


def test_legacy_sql_enforces_every_union_branch_and_time_operator():
    a,b=TABLE_BY_TYPE["threshold"],TABLE_BY_TYPE["trend"]
    spec=AlarmQuerySpec(equip_no="E1",alarm_types=["threshold","trend"],start_time=datetime(2026,9,1))
    good="equip_no = 'E1' AND latest_end_time IS NULL AND latest_start_time >= '2026-09-01 00:00:00'"
    sql=f"SELECT * FROM (SELECT COUNT(*) AS n FROM {a} WHERE {good} UNION ALL SELECT COUNT(*) AS n FROM {b} WHERE {good}) x LIMIT 1"
    clean=validate_read_only_sql(sql,{a,b},10);validate_required_filters(clean,spec)
    with pytest.raises(ValueError):validate_required_filters(sql.replace(good,"1=1",1),spec)
    with pytest.raises(ValueError):validate_required_filters(sql.replace(" >= "," <= "),spec)
    with pytest.raises(ValueError):validate_required_filters(sql.replace(good,"("+good+") OR 1=1"),spec)
