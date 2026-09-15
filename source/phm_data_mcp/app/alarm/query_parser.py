from __future__ import annotations

import re

from app.alarm.constants import TABLE_BY_TYPE
from app.models import AlarmQuerySpec
from app.time_utils import parse_time


def _alarm_types(query: str, explicit: list[str] | None) -> list[str]:
    if explicit:
        result = [x.lower() for x in explicit if x.lower() in TABLE_BY_TYPE]
        return result or list(TABLE_BY_TYPE)
    found: list[str] = []
    checks = [
        ("threshold", r"阈值|越限"),
        ("trend", r"趋势"),
        ("diagnosis", r"机理|诊断"),
        ("ai", r"(?i)(?:\bAI\b|AI报警|智能模型报警|人工智能报警)"),
    ]
    for code, pattern in checks:
        if re.search(pattern, query):
            found.append(code)
    return found or list(TABLE_BY_TYPE)


def _limit(query: str, explicit: int) -> int:
    if explicit != 20:
        return max(1, min(200, explicit))
    if re.search(r"最近一条|最新一条|最后一条|最早一条|最近一次|最新一次", query):
        return 1
    match = re.search(r"(?:前|最近|最新|最多(?:的)?)\s*(\d+)\s*(?:条|个|台|项|名|次)", query)
    return max(1, min(200, int(match.group(1)))) if match else 20


def _metric(query: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    if re.search(r"报警设备数|受影响设备数|多少台(?:设备)?|几台(?:设备)?|设备数量", query):
        return "distinct_equipment"
    if re.search(r"累计报警次数|报警总数|报警总次数|发生次数|累计次数|总共.*次", query):
        return "sum_occurrences"
    if re.search(r"多少条|几条|记录数|报警数量|报警条数|统计|汇总|分布|排行|排名|报警最多", query):
        return "count_records"
    return "detail"


def _group(query: str, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    checks = [
        ("alarm_type", r"各(?:类|种)报警|按报警类型|报警类型(?:分布|统计|排行)"),
        ("equipment", r"各设备|每台设备|按设备|设备(?:排行|排名|分布)|报警最多(?:的)?(?:\d+台)?设备|报警最多(?:的)?\d+台"),
        ("space", r"各区域|各车间|各产线|按区域|按车间|按产线|区域(?:排行|排名|分布)"),
        ("warn_level", r"各报警等级|按报警等级|按等级|等级分布"),
        ("model", r"各模型|按模型|模型(?:排行|分布)"),
        ("deal_status", r"按处理状态|处理状态分布|各处理状态"),
        ("confirm_status", r"按确认状态|确认状态分布|各确认状态"),
    ]
    for code, pattern in checks:
        if re.search(pattern, query):
            return code
    return None



def build_spec(
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
) -> AlarmQuerySpec:
    q = (query or "").strip()
    # Natural-language time semantics are intentionally NOT parsed in Data MCP.
    # Conversation Supervisor resolves relative expressions against its live runtime
    # clock and sends absolute structured timestamps. Data MCP only validates those
    # explicit parameters.
    start = parse_time(start_time) if start_time else None
    end = parse_time(end_time) if end_time else None
    target = parse_time(target_time) if target_time else None
    mode = str(time_mode or "default")
    if mode == "default" and (start is not None or end is not None):
        mode = "range"

    if mode not in {"default", "latest", "nearest", "latest_before", "range"}:
        raise ValueError("time_mode只支持default、latest、nearest、latest_before、range")
    if mode in {"nearest", "latest_before"} and target is None:
        raise ValueError(f"time_mode={mode}时必须提供target_time")
    if mode == "range" and start is None and end is None:
        raise ValueError("time_mode=range时必须提供start_time或end_time")
    if start and end and start > end:
        raise ValueError("start_time不能晚于end_time")

    current_terms = bool(re.search(r"当前|正在|实时|未结束|未恢复|持续中|有效报警", q))
    ended_terms = bool(re.search(r"已结束|已恢复|已消除|已关闭|历史报警|历史记录|曾经", q))
    if current_terms:
        state = "active"
    elif ended_terms:
        state = "ended"
    elif mode in ("latest", "nearest", "latest_before", "range") or start or end:
        state = "all"
    else:
        # Preserve the existing YML business rule: no explicit time/history means current alarms.
        state = "active"

    types = _alarm_types(q, alarm_types)
    if point_no:
        types = [x for x in types if x in ("threshold", "trend")]

    return AlarmQuerySpec(
        query=q,
        alarm_types=types,
        equip_no=equip_no,
        equip_name=equip_name,
        equip_name_keyword=equip_name_keyword,
        point_no=point_no,
        model_no=model_no,
        space_link=space_link,
        space_name=space_name,
        start_time=start,
        end_time=end,
        target_time=target,
        time_mode=mode,
        alarm_state=state,
        metric=_metric(q, metric),
        group_by=_group(q, group_by),
        limit=_limit(q, limit),
    )
