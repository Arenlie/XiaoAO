from __future__ import annotations

import re
from typing import Any

from app.alarm.constants import TABLE_BY_TYPE, TYPE_NAME
from app.models import AlarmQuerySpec


DETAIL_FIELDS = [
    "id", "equip_no", "space_link", "space_name", "equip_id", "equip_name",
    "model_no", "model_name", "warn_level", "warn_level_ch", "total_num",
    "deal_status_ch", "confirm_status_ch", "start_time", "latest_start_time",
    "latest_end_time", "duration", "restrain_flag", "restrain_reason",
]


def needs_llm(spec: AlarmQuerySpec) -> str | None:
    q = spec.query
    if re.search(r"平均|均值|占比|比例|百分比|环比|同比|对比|中位数|方差|标准差", q):
        return "涉及平均值、比例或对比计算"
    if re.search(r"最长持续|最短持续|平均持续|持续时间|时长", q):
        return "涉及持续时间复杂统计"
    if re.search(r"按小时|按天|按日|按周|按月|时间分布|时间趋势", q):
        return "涉及时间桶分组"
    if spec.group_by not in (None, "alarm_type", "equipment", "space", "warn_level", "model", "deal_status", "confirm_status"):
        return "无法识别分组维度"
    if spec.metric not in ("detail", "count_records", "sum_occurrences", "distinct_equipment"):
        return "无法识别统计方式"
    return None


def _filters(spec: AlarmQuerySpec) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    exact = {
        "equip_no": spec.equip_no,
        "equip_name": spec.equip_name,
        "point_no": spec.point_no,
        "model_no": spec.model_no,
    }
    for column, value in exact.items():
        if value:
            clauses.append(f"{column} = %s")
            params.append(value)
    if spec.equip_name_keyword:
        clauses.append("equip_name LIKE %s")
        params.append("%" + spec.equip_name_keyword + "%")

    if spec.space_link:
        clauses.append("space_link LIKE %s")
        params.append(spec.space_link + "%")
    elif spec.space_name:
        clauses.append("space_name LIKE %s")
        params.append("%" + spec.space_name + "%")

    if spec.alarm_state == "active":
        clauses.append("latest_end_time IS NULL")
    elif spec.alarm_state == "ended":
        clauses.append("latest_end_time IS NOT NULL")

    if spec.start_time:
        clauses.append("latest_start_time >= %s")
        params.append(spec.start_time.strftime("%Y-%m-%d %H:%M:%S"))
    if spec.end_time:
        clauses.append("latest_start_time < %s" if spec.end_exclusive else "latest_start_time <= %s")
        params.append(spec.end_time.strftime("%Y-%m-%d %H:%M:%S"))

    q = spec.query
    if "未处理" in q:
        clauses.append("deal_status_ch = %s"); params.append("未处理")
    elif "处理中" in q:
        clauses.append("deal_status_ch = %s"); params.append("处理中")
    elif "已处理" in q:
        clauses.append("deal_status_ch = %s"); params.append("已处理")
    if "未确认" in q:
        clauses.append("confirm_status_ch = %s"); params.append("未确认")
    elif "已确认" in q:
        clauses.append("confirm_status_ch = %s"); params.append("已确认")
    if re.search(r"未抑制|没有抑制|未开启抑制|取消抑制", q):
        clauses.append("restrain_flag = 0")
    elif re.search(r"抑制中|已抑制|开启抑制|受抑制", q):
        clauses.append("restrain_flag = 1")
    return clauses, params


def _detail_branch(alarm_type: str, where: str) -> str:
    point = (
        "point_no"
        if alarm_type in ("threshold", "trend")
        else (
            "CAST(NULL AS CHAR CHARACTER SET utf8mb4) "
            "COLLATE utf8mb4_general_ci AS point_no"
        )
    )
    selected = [
        f"'{alarm_type}' AS alarm_type",
        f"'{TYPE_NAME[alarm_type]}' AS alarm_type_name",
        "id", "equip_no", point,
        *[x for x in DETAIL_FIELDS if x not in ("id", "equip_no")],
    ]
    return "SELECT " + ", ".join(selected) + f" FROM {TABLE_BY_TYPE[alarm_type]}" + where


def _group_fields(group: str) -> list[str]:
    return {
        "alarm_type": ["alarm_type", "alarm_type_name"],
        "equipment": ["equip_no", "equip_name"],
        "space": ["space_link", "space_name"],
        "warn_level": ["warn_level", "warn_level_ch"],
        "model": ["model_no", "model_name"],
        "deal_status": ["deal_status_ch"],
        "confirm_status": ["confirm_status_ch"],
    }[group]


def build_sql(spec: AlarmQuerySpec) -> tuple[str, list[Any]]:
    clauses, base_params = _filters(spec)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    types = spec.alarm_types

    if spec.group_by:
        group = spec.group_by
        fields = _group_fields(group)
        branches: list[str] = []
        params: list[Any] = []
        if spec.metric == "distinct_equipment":
            for alarm_type in types:
                selected=[f"'{alarm_type}' AS alarm_type",f"'{TYPE_NAME[alarm_type]}' AS alarm_type_name"] if group=="alarm_type" else list(fields)
                if "equip_no" not in fields:selected.append("equip_no")
                branches.append("SELECT "+", ".join(selected)+f" FROM {TABLE_BY_TYPE[alarm_type]}"+where)
                params.extend(base_params)
            return (f"SELECT {', '.join(fields)}, COUNT(DISTINCT equip_no) AS equipment_count FROM ({' UNION ALL '.join(branches)}) alarm_groups GROUP BY {', '.join(fields)} ORDER BY equipment_count DESC LIMIT {spec.limit}",params)
        for alarm_type in types:
            select_fields = fields
            if group == "alarm_type":
                select_fields = [f"'{alarm_type}' AS alarm_type", f"'{TYPE_NAME[alarm_type]}' AS alarm_type_name"]
            expression = {
                "count_records": "COUNT(*)",
                "sum_occurrences": "COALESCE(SUM(total_num),0)",
                "distinct_equipment": "COUNT(DISTINCT equip_no)",
                "detail": "COUNT(*)",
            }[spec.metric]
            group_sql = "" if group == "alarm_type" else " GROUP BY " + ", ".join(fields)
            branches.append("SELECT " + ", ".join(select_fields) + f", {expression} AS metric_value FROM {TABLE_BY_TYPE[alarm_type]}" + where + group_sql)
            params.extend(base_params)
        inner = " UNION ALL ".join(branches)
        alias = {"sum_occurrences": "total_alarm_count", "distinct_equipment": "equipment_count"}.get(spec.metric, "alarm_count")
        sql = f"SELECT {', '.join(fields)}, SUM(metric_value) AS {alias} FROM ({inner}) alarm_groups GROUP BY {', '.join(fields)} ORDER BY {alias} DESC LIMIT {spec.limit}"
        return sql, params

    if spec.metric in ("count_records", "sum_occurrences"):
        expression = "COUNT(*)" if spec.metric == "count_records" else "COALESCE(SUM(total_num),0)"
        alias = "alarm_count" if spec.metric == "count_records" else "total_alarm_count"
        branches = [f"SELECT {expression} AS metric_value FROM {TABLE_BY_TYPE[t]}{where}" for t in types]
        params = base_params * len(types)
        return f"SELECT SUM(metric_value) AS {alias} FROM ({' UNION ALL '.join(branches)}) alarm_counts LIMIT 1", params

    if spec.metric == "distinct_equipment":
        branches = [f"SELECT DISTINCT equip_no FROM {TABLE_BY_TYPE[t]}{where}" for t in types]
        params = base_params * len(types)
        return f"SELECT COUNT(DISTINCT equip_no) AS equipment_count FROM ({' UNION ALL '.join(branches)}) alarm_equipment LIMIT 1", params

    branches = [_detail_branch(t, where) for t in types]
    params = base_params * len(types)
    inner = " UNION ALL ".join(branches)
    if spec.time_mode == "nearest" and spec.target_time:
        sql = f"SELECT * FROM ({inner}) alarm_records ORDER BY ABS(TIMESTAMPDIFF(SECOND, latest_start_time, %s)) ASC LIMIT {spec.limit}"
        params.append(spec.target_time.strftime("%Y-%m-%d %H:%M:%S"))
    elif spec.time_mode == "latest_before" and spec.target_time:
        # Apply target-time condition outside the union so every alarm type uses the same rule.
        sql = f"SELECT * FROM ({inner}) alarm_records WHERE latest_start_time <= %s ORDER BY latest_start_time DESC LIMIT {spec.limit}"
        params.append(spec.target_time.strftime("%Y-%m-%d %H:%M:%S"))
    else:
        order = "ASC" if re.search(r"最早|从早到晚|时间升序", spec.query) else "DESC"
        sql = f"SELECT * FROM ({inner}) alarm_records ORDER BY latest_start_time {order} LIMIT {spec.limit}"
    return sql, params
