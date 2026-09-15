from __future__ import annotations

from typing import Any, Mapping

from app.config import get_settings
from app.domain.phm_point_codes import (
    TEMPERATURE_KPI_ID,
    VIBRATION_FEATURE_KPI_IDS,
    enrich_phm_point_entity,
)
from app.tools.alarm_context import infer_alarm_equipment_keyword
from app.tools.health_context import (
    health_query_requests_history,
    infer_health_scope_type,
    infer_health_time_range,
)
from app.tools.phm_data_mcp import (
    PHM_CHECK_DATA_AVAILABILITY_TOOL_ID,
    PHM_GET_DATA_SNAPSHOT_TOOL_ID,
    PHM_GET_DEVICE_DATA_TOOL_ID,
    PHM_GET_FEATURE_TREND_TOOL_ID,
    PHM_GET_TEMPERATURE_TREND_TOOL_ID,
    PHM_GET_WAVEFORM_TOOL_ID,
    PHM_QUERY_ALARM_RECORDS_TOOL_ID,
    PHM_QUERY_HEALTH_SCORE_TOOL_ID,
)
from app.tools.time_validation import (
    validate_absolute_range_arguments,
    validate_alarm_time_arguments,
)


_CONTROL_ARGUMENTS = {"objective", "required_entity_level"}


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _merge_entity(value: Mapping[str, Any] | None) -> dict[str, Any]:
    source = _as_dict(value)
    metadata = source.get("metadata")
    if isinstance(metadata, Mapping):
        source = {**dict(metadata), **source}
    return enrich_phm_point_entity(source)


def _first(source: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = source.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _entity_from_state(state: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("selected_entity", "resolved_entity", "active_entity"):
        item = state.get(key)
        if isinstance(item, Mapping) and item:
            return _merge_entity(item)
    diagnosis = state.get("active_diagnosis_task")
    if isinstance(diagnosis, Mapping):
        item = diagnosis.get("resolved_entity")
        if isinstance(item, Mapping) and item:
            return _merge_entity(item)
    return {}


def _diagnosis_context(state: Mapping[str, Any]) -> dict[str, Any]:
    value = state.get("active_diagnosis_task")
    return _as_dict(value)


def _query_scope(state: Mapping[str, Any]) -> dict[str, Any]:
    direct = state.get("query_scope")
    if isinstance(direct, Mapping) and direct:
        return dict(direct)
    result = state.get("entity_result")
    if isinstance(result, Mapping) and isinstance(result.get("query_scope"), Mapping):
        return dict(result["query_scope"])
    return {}


def _entity_constraints(state: Mapping[str, Any]) -> dict[str, Any]:
    direct = state.get("entity_constraints")
    if isinstance(direct, Mapping) and direct:
        return dict(direct)
    result = state.get("entity_result")
    if isinstance(result, Mapping) and isinstance(result.get("entity_constraints"), Mapping):
        return dict(result["entity_constraints"])
    return {}


def resolve_phm_identity_fields(state: Mapping[str, Any]) -> dict[str, Any]:
    entity = _entity_from_state(state)
    diagnosis = _diagnosis_context(state)
    diagnosis_entity = _merge_entity(
        diagnosis.get("resolved_entity") if isinstance(diagnosis.get("resolved_entity"), Mapping) else {}
    )
    merged = enrich_phm_point_entity({**diagnosis_entity, **entity})
    return {
        "entity_type": _first(merged, "entity_type", "match_type", "leaf_space_type"),
        "device_id": _first(merged, "device_id", "deviceId", "equip_no", "equipment_no", "device_code", "equipNo"),
        "space_id": _first(merged, "space_id", "spaceId"),
        "device_code": _first(
            merged,
            "device_code",
            "equip_no",
            "equipment_no",
            "equipNo",
        ),
        "wave_point_no": _first(
            merged,
            "wave_point_no",
            "wave_point_code",
            "point_no",
            "pointNo",
        ),
        "temperature_point_no": _first(
            merged,
            "temperature_point_no",
            "temperature_point_code",
        ),
        "feature_point_id": _first(
            merged,
            "feature_point_id",
            "featurePointId",
            "point_id",
            "pointId",
        ),
        "temperature_point_id": _first(
            merged,
            "temperature_point_id",
            "temperaturePointId",
        ),
        "vibration_feature_codes": _first(merged, "vibration_feature_codes") or {},
        "vibration_feature_code_list": _first(merged, "vibration_feature_code_list") or [],
        "temperature_feature_code": _first(merged, "temperature_feature_code"),
        "equip_no": _first(merged, "equip_no", "equipment_no", "device_code", "equipNo"),
        "equip_name": _first(merged, "equip_name", "equipment_name"),
        # Keep the original fuzzy-entity point_no for alarm filtering/public context.
        "point_no": _first(merged, "point_no", "pointNo", "wave_point_no", "wave_point_code"),
        "space_link": _first(merged, "space_link", "spaceLink"),
        "space_name": _first(merged, "space_name", "area_name", "leaf_space_name"),
        "model_no": _first(merged, "model_no", "modelNo"),
    }


def _clean_model_arguments(call_arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in call_arguments.items()
        if key not in _CONTROL_ARGUMENTS and value not in (None, "", [], {})
    }


def _apply_alarm_scope(arguments: dict[str, Any], state: Mapping[str, Any]) -> None:
    ids = resolve_phm_identity_fields(state)
    query_scope = _query_scope(state)
    constraints = _entity_constraints(state)
    resolved_area = _as_dict(query_scope.get("resolved_area"))
    equipment_filters = _as_dict(query_scope.get("equipment_filters"))

    # Identity is platform-owned. Never preserve model-generated or previous-turn
    # equipment/point/space filters in an alarm call: a scope transition such as
    # equipment -> business unit must clear the lower-level filter completely.
    for key in (
        "equip_no", "equip_name", "point_no", "space_link", "space_name",
        "equip_name_keyword",
    ):
        arguments.pop(key, None)

    if ids["equip_no"]:
        arguments["equip_no"] = ids["equip_no"]
    if ids["equip_name"]:
        arguments["equip_name"] = ids["equip_name"]
    if ids["point_no"]:
        arguments["point_no"] = ids["point_no"]
    space_link = ids["space_link"] or _first(resolved_area, "space_link", "spaceLink")
    if space_link:
        arguments["space_link"] = space_link
    space_name = ids["space_name"] or _first(resolved_area, "space_name", "area_name")
    if space_name:
        arguments["space_name"] = space_name

    # Equipment-type filtering is turn-local. Never reuse an old query_scope filter
    # after the user broadens from a device to its whole business unit.
    equipment_keyword = infer_alarm_equipment_keyword(str(state.get("query") or ""))
    if equipment_keyword and not arguments.get("equip_no"):
        arguments["equip_name_keyword"] = equipment_keyword


def _normalize_vibration_kpi_ids(
    values: Any,
    ids: Mapping[str, Any],
) -> list[str]:
    """Return Data MCP kpiId values as full PHM feature codes.

    Mongo eige_* stores kpiId as the complete feature code (for example
    ``<wave_point_no>001``), not the three-digit suffix alone.  Accept short
    planner/user suffixes for compatibility, but expand them deterministically.
    """
    wave_point_no = str(ids.get("wave_point_no") or "").strip()
    derived = [
        str(item).strip()
        for item in (ids.get("vibration_feature_code_list") or [])
        if str(item).strip()
    ]

    if values in (None, "", [], {}):
        if derived:
            return derived
        if wave_point_no:
            return [f"{wave_point_no}{suffix}" for suffix in VIBRATION_FEATURE_KPI_IDS]
        return []

    raw_values = values if isinstance(values, (list, tuple, set)) else [values]
    result: list[str] = []
    for value in raw_values:
        text = str(value).strip()
        if not text:
            continue
        if len(text) == 3 and text in VIBRATION_FEATURE_KPI_IDS and wave_point_no:
            text = f"{wave_point_no}{text}"
        if text not in result:
            result.append(text)
    return result


def _normalize_temperature_kpi_id(value: Any, ids: Mapping[str, Any]) -> str | None:
    """Return Data MCP temperature kpiId as the full ``<T-point>000`` code."""
    temperature_point_id = str(ids.get("temperature_point_id") or "").strip()
    derived = str(ids.get("temperature_feature_code") or "").strip()
    if value not in (None, "", [], {}):
        text = str(value).strip()
        if text == TEMPERATURE_KPI_ID and temperature_point_id:
            return f"{temperature_point_id}{TEMPERATURE_KPI_ID}"
        return text or None
    if derived:
        return derived
    # For legacy non-A/T entities, omit kpi_id and let Data MCP apply its documented
    # default ``{point_id}000`` instead of sending the invalid short literal "000".
    return None


def build_phm_data_arguments(
    *,
    tool_id: str,
    state: Mapping[str, Any],
    call_arguments: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    arguments = _clean_model_arguments(call_arguments)
    ids = resolve_phm_identity_fields(state)
    missing: list[str] = []

    if tool_id == PHM_QUERY_ALARM_RECORDS_TOOL_ID:
        arguments.setdefault("query", str(state.get("query") or ""))
        _apply_alarm_scope(arguments, state)
        # Time semantics are already resolved by the Supervisor LLM against the live
        # runtime clock. The backend only validates/normalizes those absolute values;
        # it never reparses Chinese temporal expressions from query text here.
        try:
            timezone_name = get_settings().business_timezone
        except Exception:
            timezone_name = "Asia/Shanghai"
        arguments = validate_alarm_time_arguments(
            arguments, timezone_name=timezone_name
        )
        return arguments, missing

    if tool_id == PHM_QUERY_HEALTH_SCORE_TOOL_ID:
        query = str(state.get("query") or "")
        entity = _entity_from_state(state)
        scope_type = infer_health_scope_type(
            query,
            arguments,
            entity,
            state.get("business_intent") if isinstance(state.get("business_intent"), Mapping) else {},
        )
        arguments["scope_type"] = scope_type
        # Never trust a model-generated scope_id. Health identity must come from the
        # resolved fuzzy-entity result so names/space_link cannot leak into the MCP call.
        arguments.pop("scope_id", None)

        if scope_type == "space":
            if ids.get("space_id"):
                arguments["scope_id"] = str(ids["space_id"])
            else:
                missing.append("space_id")
            # Region health is realtime-only. Keep any history markers for the executor
            # guard to reject before MCP invocation, but never add synthetic time values.
            arguments.pop("limit", None)
            return arguments, missing

        device_scope_id = ids.get("device_id") or ids.get("equip_no") or ids.get("device_code")
        if device_scope_id:
            arguments["scope_id"] = str(device_scope_id)
        else:
            missing.append("device_id/equip_no")

        if health_query_requests_history(query, arguments):
            try:
                timezone_name = get_settings().business_timezone
            except Exception:
                timezone_name = "Asia/Shanghai"
            arguments = validate_absolute_range_arguments(
                arguments, timezone_name=timezone_name
            )
            if arguments.get("start_time") or arguments.get("end_time"):
                arguments["limit"] = 100
        else:
            # Current device health must use the MCP latest-record path. Do not turn it
            # into a 100-row history query and then pick the first record.
            arguments.pop("start_time", None)
            arguments.pop("end_time", None)
            arguments.pop("limit", None)
        return arguments, missing

    if tool_id == PHM_GET_WAVEFORM_TOOL_ID:
        if ids["device_code"]:
            arguments["device_code"] = ids["device_code"]
        else:
            missing.append("device_code/equip_no")
        if ids["wave_point_no"]:
            arguments["point_no"] = ids["wave_point_no"]
        else:
            missing.append("point_no")
        arguments.setdefault("mode", "latest")
        return arguments, missing

    if tool_id == PHM_GET_FEATURE_TREND_TOOL_ID:
        if ids["device_code"]:
            arguments["device_code"] = ids["device_code"]
        else:
            missing.append("device_code/equip_no")
        if ids["feature_point_id"]:
            arguments["point_id"] = ids["feature_point_id"]
        else:
            missing.append("feature_point_id/point_id")
        # Data MCP/Mongo kpiId is the full feature code (A001...A008), not only
        # the three-digit suffix. Short explicit suffixes are expanded for compatibility.
        kpi_ids = _normalize_vibration_kpi_ids(arguments.get("kpi_ids"), ids)
        if kpi_ids:
            arguments["kpi_ids"] = kpi_ids
        else:
            arguments.pop("kpi_ids", None)
        arguments.setdefault("return_mode", "auto")
        return arguments, missing

    if tool_id == PHM_GET_TEMPERATURE_TREND_TOOL_ID:
        if ids["device_code"]:
            arguments["device_code"] = ids["device_code"]
        else:
            missing.append("device_code/equip_no")
        temperature_point_id = ids["temperature_point_id"] or ids["feature_point_id"]
        if temperature_point_id:
            arguments["point_id"] = temperature_point_id
        else:
            missing.append("temperature_point_id/feature_point_id/point_id")
        kpi_id = _normalize_temperature_kpi_id(arguments.get("kpi_id"), ids)
        if kpi_id:
            arguments["kpi_id"] = kpi_id
        else:
            arguments.pop("kpi_id", None)
        arguments.setdefault("return_mode", "auto")
        return arguments, missing

    if tool_id == PHM_CHECK_DATA_AVAILABILITY_TOOL_ID:
        if ids["device_code"]:
            arguments["device_code"] = ids["device_code"]
        else:
            missing.append("device_code/equip_no")
        if ids["wave_point_no"]:
            arguments["wave_point_no"] = ids["wave_point_no"]
        if ids["feature_point_id"]:
            arguments["feature_point_id"] = ids["feature_point_id"]
        if not arguments.get("wave_point_no") and not arguments.get("feature_point_id"):
            missing.append("wave_point_no 或 feature_point_id")
        return arguments, missing

    if tool_id == PHM_GET_DEVICE_DATA_TOOL_ID:
        if ids["device_code"]:
            arguments["device_code"] = ids["device_code"]
        else:
            missing.append("device_code/equip_no")
        if not arguments.get("trend_hours") and not arguments.get("trend_days"):
            arguments["trend_days"] = 30
        arguments.setdefault("search_window_seconds", 86400)
        return arguments, missing

    if tool_id == PHM_GET_DATA_SNAPSHOT_TOOL_ID:
        if ids["device_code"]:
            arguments["device_code"] = ids["device_code"]
        else:
            missing.append("device_code/equip_no")
        if ids["wave_point_no"]:
            arguments["wave_point_no"] = ids["wave_point_no"]
        if ids["feature_point_id"]:
            arguments["feature_point_id"] = ids["feature_point_id"]
        if not arguments.get("wave_point_no") and not arguments.get("feature_point_id"):
            missing.append("wave_point_no 或 feature_point_id")
        kpi_ids = _normalize_vibration_kpi_ids(arguments.get("kpi_ids"), ids)
        if kpi_ids:
            arguments["kpi_ids"] = kpi_ids
        else:
            arguments.pop("kpi_ids", None)
        if not arguments.get("trend_hours") and not arguments.get("trend_days"):
            arguments["trend_days"] = 30
        arguments.setdefault("tolerance_seconds", 3600)
        arguments.setdefault("search_window_seconds", 86400)
        return arguments, missing

    return arguments, missing


def _format_health_timestamp(value: Any) -> str:
    """Format health ts for model-visible evidence without changing MCP data."""
    if value in (None, ""):
        return "未提供"
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        if isinstance(value, (int, float)):
            numeric = float(value)
            # Health Mongo stores epoch milliseconds in current production data.
            if numeric > 10_000_000_000:
                numeric /= 1000.0
            return datetime.fromtimestamp(numeric, ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    except Exception:
        pass
    return str(value)


def _health_score(record: Mapping[str, Any]) -> Any:
    return _first(record, "score", "total_score", "totalScore", "finalScore")


def format_health_observation(
    payload: Mapping[str, Any],
    *,
    query: str = "",
    call_arguments: Mapping[str, Any] | None = None,
) -> str:
    """Create deterministic model-visible health evidence.

    In particular, a historical MCP response is explicitly labelled as a sequence so a
    synthesis model cannot silently collapse a requested 7/30-day range into only the
    newest record. The original structured payload remains attached as evidence.
    """

    source = dict(payload or {})
    data = source.get("data") if "data" in source else source
    requested = dict(call_arguments or {})
    requested_start = str(requested.get("start_time") or "").strip()
    requested_end = str(requested.get("end_time") or "").strip()
    requested_range = (
        f"查询时间范围：{requested_start} ～ {requested_end}。"
        if requested_start or requested_end
        else ""
    )
    if isinstance(data, list):
        records = [item for item in data if isinstance(item, Mapping)]
        if not records:
            base = "设备历史健康度查询已执行，但指定时间范围内未返回健康度记录。"
            return f"{base}\n{requested_range}" if requested_range else base
        timestamps = [_format_health_timestamp(item.get("ts")) for item in records]
        scores = [
            value for value in (_health_score(item) for item in records)
            if isinstance(value, (int, float))
        ]
        lines = [f"设备历史健康度查询返回 {len(records)} 条记录。"]
        if requested_range:
            lines.append(requested_range)
        lines.append(
            f"返回记录时间范围：{timestamps[-1]} ～ {timestamps[0]}（工具按时间倒序返回）。"
        )
        if scores:
            lines.append(f"健康分范围：最低 {min(scores)}，最高 {max(scores)}。")
        # Keep a bounded readable trace in answer_markdown; the full list remains in
        # structured evidence for synthesis/audit.
        preview = records[: min(len(records), 30)]
        lines.append("历史记录：")
        for item in preview:
            score = _health_score(item)
            grade = item.get("grade")
            lines.append(
                f"- {_format_health_timestamp(item.get('ts'))}：健康分 {score if score is not None else '未提供'}，"
                f"健康等级 {grade if grade not in (None, '') else '未提供'}"
            )
        if len(records) > len(preview):
            lines.append(f"- 其余 {len(records) - len(preview)} 条记录保留在结构化证据中。")
        return "\n".join(lines)

    if isinstance(data, Mapping):
        score = _health_score(data)
        grade = data.get("grade")
        ts = _format_health_timestamp(data.get("ts"))
        parts = [
            f"健康分：{score if score is not None else '未提供'}",
            f"健康等级：{grade if grade not in (None, '') else '未提供'}",
            f"评估时间：{ts}",
        ]
        for key, label in (
            ("thresholdScore", "阈值模型得分"),
            ("trendScore", "趋势模型得分"),
            ("aiScore", "AI模型得分"),
            ("mechanismScore", "机理模型得分"),
        ):
            if data.get(key) not in (None, ""):
                parts.append(f"{label}：{data.get(key)}")
        return "\n".join(parts)
    return "健康度查询已执行，但返回结构无法识别。"
