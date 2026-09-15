from __future__ import annotations

from typing import Any

from app.alarm.constants import TABLE_BY_TYPE
from app.alarm.fast_sql import build_sql, needs_llm
from app.alarm.llm_sql import AlarmSqlPlanner
from app.alarm.query_parser import build_spec
from app.alarm.sql_validator import validate_read_only_sql, validate_required_filters
from app.config import Settings
from app.repositories.mysql import MySqlRepository


class AlarmService:
    def __init__(self, settings: Settings, mysql: MySqlRepository):
        self.settings = settings
        self.mysql = mysql
        self.llm = AlarmSqlPlanner(settings)

    def query(
        self,
        *,
        query: str = "",
        alarm_types: list[str] | None = None,
        equip_no: str | None = None,
        equip_name: str | None = None,
        equip_name_keyword: str | None = None,
        point_no: str | None = None,
        model_no: str | None = None,
        space_link: str | None = None,
        space_name: str | None = None,
        time_mode: str = "default",
        target_time: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        metric: str | None = None,
        group_by: str | None = None,
        limit: int = 20,
        alarm_state: str | None = None,
        end_exclusive: bool = False,
    ) -> dict[str, Any]:
        spec = build_spec(
            query=query,
            alarm_types=alarm_types,
            equip_no=equip_no,
            equip_name=equip_name,
            equip_name_keyword=equip_name_keyword,
            point_no=point_no,
            model_no=model_no,
            space_link=space_link,
            space_name=space_name,
            time_mode=time_mode,
            target_time=target_time,
            start_time=start_time,
            end_time=end_time,
            metric=metric,
            group_by=group_by,
            limit=limit,
        )
        spec.end_exclusive=end_exclusive
        if alarm_state is not None:
            if alarm_state not in {"all","active","ended"}:raise ValueError("报警状态参数无效")
            spec.alarm_state=alarm_state
        if not spec.alarm_types:
            return {"success": True, "data": [], "decode": {"required": False}}

        fallback_reason = needs_llm(spec)
        if fallback_reason:
            raw_sql = self.llm.generate(spec)
            expected = {TABLE_BY_TYPE[x] for x in spec.alarm_types}
            sql = validate_read_only_sql(raw_sql, expected, spec.limit)
            validate_required_filters(sql, spec)
            params: list[Any] = []
            query_mode = "llm_sql"
        else:
            sql, params = build_sql(spec)
            query_mode = "rule_sql"

        if query_mode == "llm_sql":
            plan = self.mysql.query("EXPLAIN " + sql, params)
            estimated=sum(int(r.get("rows") or 0) for r in plan)
            if estimated > 1_000_000:
                raise ValueError("高级报警查询预计扫描量超过预算，请缩小时间或对象范围")
        rows = self.mysql.query(sql, params)
        return {
            "success": True,
            "data": rows,
            "decode": {"required": False},
            "_audit": {"sql": sql, "row_count": len(rows), "query_mode": query_mode},
        }
