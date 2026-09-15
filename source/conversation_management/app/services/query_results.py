"""Durable answer sets stored on their owning message; no process-global last result."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4
import math


def identity(row):
    source = dict(row or {})
    value = {**(source.get("metadata") or {}), **source}
    entity = source.get("entity")
    if isinstance(entity, dict):
        value.update({**(entity.get("metadata") or {}), **entity})
    kind = value.get("entity_type") or ("point" if value.get("point_no") else "equipment" if value.get("equip_no") else "space")
    key = value.get("entity_key") or value.get("point_no") or value.get("equip_no") or value.get("space_id")
    name = (value.get("point_name") if kind == "point" else value.get("equip_name") or value.get("equipment_name") if kind == "equipment" else value.get("space_name"))
    return {"entity_type": kind, "entity_key": str(key or ""),
            "name": name or value.get("display_name") or value.get("name") or str(key or ""),
            **{k: value[k] for k in ("equip_no", "point_no", "space_id", "space_link", "space_path", "parent_space_id") if value.get(k)},
            "area": value.get("space_path") or value.get("path") or value.get("area_name") or value.get("leaf_space_name") or "",
            "equipment_type": "、".join(str(x) for x in value.get("equipment_classes") or []) or value.get("equipment_type") or value.get("equip_type") or "",
            "model": value.get("equipment_model") or value.get("model") or ""}


def health_fact(raw):
    data = raw.get("data", raw) if isinstance(raw, dict) else raw
    if isinstance(data, list):
        data = data[0] if data else {}
    data = data if isinstance(data, dict) else {}
    score = next((data[k] for k in ("score", "total_score", "finalScore", "health_score") if data.get(k) is not None), None)
    valid = isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score)
    grade = data.get("grade")
    grade = {1:"重点关注",2:"早期关注",3:"良好",4:"优秀",5:"离线"}.get(grade, grade)
    stamp = data.get("ts") or data.get("timestamp") or data.get("data_time")
    if isinstance(stamp, (int, float)):
        from zoneinfo import ZoneInfo
        stamp = datetime.fromtimestamp(stamp / 1000 if abs(stamp) > 100000000000 else stamp, timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).isoformat()
    return {"health_score": score if valid else None, "grade": grade, "health_time": stamp,
            "health_available": valid and grade != "离线",
            "health_dimensions": {k: data[k] for k in ("thresholdScore", "trendScore", "aiScore", "mechanismScore", "threshold_score", "trend_score", "ai_score", "mechanism_score") if k in data}}


def make_result(*, plan, rows, root=None, complete=True, total=None, notes=None, source="platform",
                row_granularity=None, source_complete="__legacy__", display_complete=None,
                query_conditions=None, data_time=None):
    if source_complete == "__legacy__":
        source_complete = bool(complete)
    if display_complete is None:
        display_complete = True
    available_fields = sorted({
        str(k) for row in rows if isinstance(row, dict)
        for k, v in row.items() if v not in (None, "", [], {})
    })
    return {"result_id": str(uuid4()), "created_at": datetime.now(timezone.utc).isoformat(),
            "plan": deepcopy(plan), "root": deepcopy(root or {}), "rows": deepcopy(rows),
            "total_count": len(rows) if total is None else total, "displayed_count": len(rows),
            "complete": bool(complete), "source_complete": source_complete,
            "display_complete": bool(display_complete), "row_granularity": row_granularity or plan.get("target") or "record",
            "available_fields": available_fields, "query_conditions": deepcopy(query_conditions or {}),
            "data_time": data_time, "notes": list(notes or []), "source": source}


def sensor_result(raw, intent):
    """Normalize a Sensor Agent result into the same durable result-set contract.

    The upstream record schema remains untouched.  This adapter only projects the
    members/fields needed for result reuse, deterministic rendering and completion
    checks.
    """
    raw = raw if isinstance(raw, dict) else {}
    intent = intent if isinstance(intent, dict) else {}
    contract = intent.get("completion_contract") if isinstance(intent.get("completion_contract"), dict) else {}
    sensor = intent.get("sensor_query") if isinstance(intent.get("sensor_query"), dict) else {}
    operation = str(sensor.get("operation") or raw.get("query_type") or "sensor").lower()
    granularity = str(contract.get("target_granularity") or "none").lower()
    if granularity not in {"space", "equipment", "point", "record"}:
        granularity = "point" if operation in {"points", "monitoring", "offline"} else "record"

    source_records = [x for x in raw.get("records") or [] if isinstance(x, dict)]
    rows = []
    for record in source_records:
        row = {
            "name": record.get("point_name") or record.get("point_num") or record.get("equip_name") or record.get("equip_num") or "",
            "equip_no": record.get("equip_num") or record.get("equip_no"),
            "equip_name": record.get("equip_name"),
            "point_no": record.get("point_num") or record.get("point_no"),
            "point_name": record.get("point_name"),
            "area": record.get("space_path") or record.get("area") or record.get("area_name"),
            "fault_id": record.get("fault_id"),
            "fault_type_name": record.get("fault_type_name") or record.get("model_name"),
            "fault_status": record.get("fault_status"),
            "fault_status_name": record.get("fault_status_name"),
            "monitoring_status": record.get("monitoring_status") or record.get("monitor_status"),
            "monitoring_status_name": record.get("monitoring_status_name") or record.get("monitor_status_name"),
            "start_time": record.get("start_time"),
            "end_time": record.get("end_time"),
            "unit": record.get("unit"),
        }
        rows.append({k: v for k, v in row.items() if v is not None})

    if granularity in {"point", "equipment"}:
        unique = {}
        counts = {}
        for row in rows:
            key = (str(row.get("equip_no") or ""), str(row.get("point_no") or "")) if granularity == "point" else (str(row.get("equip_no") or ""),)
            if not any(key):
                continue
            counts[key] = counts.get(key, 0) + 1
            if key not in unique:
                unique[key] = dict(row)
        rows = []
        for key, row in unique.items():
            if counts[key] > 1:
                row["record_count"] = counts[key]
            rows.append(row)

    summary = raw.get("summary") if isinstance(raw.get("summary"), dict) else {}
    if granularity == "point":
        total = summary.get("unique_point_count_in_matched")
    elif granularity == "equipment":
        total = summary.get("unique_equipment_count_in_matched")
    else:
        total = summary.get("matched_count_in_fetched")
    if total is None:
        total = len(rows)

    source_meta = raw.get("source") if isinstance(raw.get("source"), dict) else {}
    source_complete = source_meta.get("complete")
    if source_meta.get("truncated_possible") is True:
        source_complete = False
    output_limited = bool(source_meta.get("output_limited"))
    returned = summary.get("returned_count")
    matched = summary.get("matched_count_in_fetched")
    display_complete = not output_limited and not (isinstance(returned, int) and isinstance(matched, int) and returned < matched)

    actions = [str(x).lower() for x in contract.get("actions") or []]
    output_type = str(contract.get("output_type") or "answer").lower()
    plan = {
        "active": True, "domain": "sensor", "target": granularity,
        "operation": "count" if output_type == "count" or "count" in actions else "list",
        "include_fields": list(contract.get("required_fields") or []),
        "display_format": output_type if output_type in {"list", "prose", "table"} else "table",
        "sensor_operation": operation,
    }
    result = make_result(
        plan=plan, rows=rows, complete=bool(source_complete is True and display_complete),
        total=total, notes=list(raw.get("warnings") or []), source="sensor_detect_agent",
        row_granularity=granularity, source_complete=source_complete, display_complete=display_complete,
        query_conditions=raw.get("filters") or {}, data_time=source_meta.get("snapshot_time"),
    )
    result["identity_enrichment"] = deepcopy(source_meta.get("asset_identity_enrichment") or {})
    return result


def summaries(results):
    return [{k: r.get(k) for k in ("result_id", "source_message_id", "created_at", "plan", "root", "row_granularity",
                                            "total_count", "displayed_count", "complete", "source_complete", "display_complete", "data_time")}
            | {"available_fields": r.get("available_fields") or sorted({k for row in r.get("rows", [])[:20] for k, v in row.items() if v is not None and v != ""}),
               "sample": [{k: x.get(k) for k in ("name", "equip_no", "point_no", "health_score", "fault_type_name")} for x in r.get("rows", [])[:5]]}
            for r in results[-8:]]


def load_results(messages):
    found = []
    for message in messages:
        if str(getattr(message, "role", "")).upper() != "ASSISTANT" or getattr(message, "status", "") != "COMPLETED":
            continue
        for row in (getattr(message, "metadata_json", None) or {}).get("answer_results", []):
            if isinstance(row, dict) and row.get("result_id"):
                found.append({**row, "source_message_id": str(message.id)})
    return found[-8:]


def resolve_result(context, request):
    rows = list(context.get("answer_results") or [])
    if request.get("source_result_id"):
        rows = [r for r in rows if r.get("result_id") == request["source_result_id"]]
    elif request.get("source_message_id"):
        rows = [r for r in rows if r.get("source_message_id") == request["source_message_id"]]
    elif rows:
        rows = [r for r in rows if r.get("source_message_id") == rows[-1].get("source_message_id")]
    if not rows:
        raise ValueError("原回答的结构化结果不可用，无法保证还原同一批对象；请明确是否重新查询。")
    if len(rows) != 1:
        raise ValueError("原回答包含多个结果，请说明要补充哪一张表或哪组对象。")
    source = deepcopy(rows[0])
    selected = (source.get("all_rows") if request.get("selection")=="all" else None) or source.get("rows") or []
    if request.get("selection") == "all" and len(selected) < source.get("total_count", len(selected)):
        # An immutable asset snapshot is a durable member-set reference.  The
        # structured follow-up/Broker may materialize it without re-querying the
        # current catalog; legacy results without such a reference remain bounded
        # to the rows actually stored on the answer.
        if not (source.get("asset_snapshot") and (source.get("plan") or {}).get("domain") == "asset"):
            raise ValueError("保存的是原回答展示的成员，不能把它冒充全部匹配对象；请重新查询完整范围。")
        source["requires_materialization"] = True
    if request.get("selection") == "ordinals":
        ordinals = request.get("ordinals") or []
        if any(i < 1 or i > len(selected) for i in ordinals):
            raise ValueError("引用序号超出了原回答实际展示的范围。")
        selected = [selected[i-1] for i in dict.fromkeys(ordinals)]
    source["rows"] = selected
    return source


def rank_rows(rows, *, key="health_score", order="asc", limit=10):
    valid = [x for x in rows if isinstance(x.get(key), (int,float)) and not isinstance(x.get(key),bool)
             and math.isfinite(x[key]) and (key != "health_score" or x.get("health_available", True))]
    ranked = sorted(valid, key=lambda x: ((-x[key] if order == "desc" else x[key]), str(x.get("entity_key") or x.get("name") or "")))
    chosen = ranked[:limit]
    ties = sum(x[key] == chosen[-1][key] for x in ranked) if chosen else 0
    shown_ties = sum(x[key] == chosen[-1][key] for x in chosen) if chosen else 0
    return chosen, {"valid_count": len(valid), "missing_count": len(rows)-len(valid),
                    "boundary_tie_count": ties, "omitted_ties": max(0, ties-shown_ties)}


def render_result(result):
    def cell(x):
        return str("暂缺" if x is None or x == "" else x).replace("|", "\\|").replace("\n", " ").replace("<", "&lt;")
    rows = result.get("rows") or []
    plan = result.get("plan") or {}
    notes = list(result.get("notes") or [])
    prefix = "\n\n".join(str(x) for x in notes if x)
    if plan.get("operation") == "count":
        label = ({"count_records":"报警记录", "sum_occurrences":"累计报警次数", "distinct_equipment":"发生报警的设备"}.get(plan.get("metric")) if plan.get("domain")=="alarm" else None) or {"equipment":"设备", "point":"测点", "space":"区域"}.get(plan.get("target"), "对象")
        return f"{prefix}\n\n符合本次查询条件的{label}数量：**{result.get('total_count', 0)}**。".strip()
    if not rows:
        return (prefix + "\n\n本次未取得可展示的匹配结果。" if not result.get("complete") else prefix + "\n\n本次查询没有匹配记录。").strip()
    labels = [("name","对象名称"),("equip_no","设备编码"),("point_no","测点编码"),("area","所在区域"),
              ("health_score","健康度"),("grade","健康等级"),("health_time","健康度数据时间"),
              ("alarm_count","报警数量"),("equipment_type","设备类型"),("model","型号"),("count","数量"),("record_count","相关记录数"),
              ("fault_type_name","故障类型"),("fault_status_name","处理状态"),("monitoring_status_name","监测状态"),
              ("start_time","开始时间"),("end_time","结束时间"),("unit","单位"),
              ("previous_alarm_count","比较时段数量"),("difference","变化数量"),("change_percent","变化比例（%）")]
    labels.extend([("alarm_type_name","报警类型"),("model_name","报警项目"),("warn_level_ch","报警等级"),
        ("latest_start_time","最近发生时间"),("latest_end_time","最近结束时间"),("total_num","累计发生次数"),("supplement_status","补充查询说明")])
    if result.get("advanced_columns"): labels=[(k,k) for k in result["advanced_columns"]]
    fields = [(k,v) for k,v in labels if any(k in row and row.get(k) not in (None, "") for row in rows)]
    # Explicitly requested missing columns remain visible instead of disappearing.
    label_map = dict(labels) | {"point_name":"测点名称", "point_no":"测点编码", "equip_name":"设备名称",
                                "equip_no":"设备编码", "space_id":"区域标识", "area":"所在区域"}
    for key in plan.get("include_fields") or []:
        if not any(k == key for k,v in fields):
            fields.append((key, label_map.get(key, key)))
    if not fields: fields = [("name","对象名称")]
    display_format = result.get("display_format") or plan.get("display_format")
    if display_format in {"list","prose"}:
        lines=[str(i)+". "+"；".join(v+"："+cell(row.get(k)) for k,v in fields) for i,row in enumerate(rows,1)]
        return "\n\n".join(x for x in (prefix,("\n" if display_format=="list" else "\n\n").join(lines)) if x)
    header = "| 序号 | " + " | ".join(v for k,v in fields) + " |\n| --- | " + " | ".join("---" for _ in fields) + " |"
    table = "\n".join("| " + str(i) + " | " + " | ".join(cell(row.get(k)) for k,v in fields) + " |" for i,row in enumerate(rows,1))
    return "\n\n".join(x for x in (prefix, header+"\n"+table) if x)
