from __future__ import annotations

from typing import Any, Mapping

from app.domain.phm_point_codes import derive_phm_point_codes, raw_measurement_point_code

from app.tools.phm_data_context import resolve_phm_identity_fields
from app.tools.phm_data_mcp import (
    PHM_GET_DATA_SNAPSHOT_TOOL_ID,
    PHM_GET_DEVICE_DATA_TOOL_ID,
    PHM_GET_FEATURE_TREND_TOOL_ID,
    PHM_GET_TEMPERATURE_TREND_TOOL_ID,
    PHM_GET_WAVEFORM_TOOL_ID,
    PHM_QUERY_ALARM_RECORDS_TOOL_ID,
)
from app.tools.phm_diagnosis_mcp import (
    ORDER_CHART_TYPES,
    PHM_DIAGNOSIS_ANALYZE_CHART_TOOL_ID,
    PHM_DIAGNOSIS_DEVICE_TOOL_ID,
    PHM_DIAGNOSIS_POINT_TOOL_ID,
    PHM_DIAGNOSIS_MODEL_ADMISSION_TOOL_ID,
    TREND_CHART_TYPES,
)
from app.tools.phm_feature_mcp import PHM_FEATURE_EXTRACT_RPM_TOOL_ID

_CONTROL_ARGUMENTS = {"objective", "required_entity_level"}
_ALLOWED_OPTIONS = {"preprocess", "median_window", "max_points"}


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _latest(transient: Mapping[str, Any], tool_id: str) -> dict[str, Any]:
    value = transient.get(f"latest:{tool_id}")
    return _as_dict(value)


def _history(transient: Mapping[str, Any], tool_id: str) -> list[dict[str, Any]]:
    value = transient.get(f"history:{tool_id}")
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    latest = _latest(transient, tool_id)
    return [latest] if latest else []


# Contract verified against phm-data-mcp 1.0.0 and phm-diagnosis-mcp 1.0.0.
# Diagnosis MCP's decoding._unwrap() deliberately accepts the nested Data MCP form
# {"data": {...}, "decode": {...}}. Preserve that wrapper instead of flattening it.
DIAGNOSIS_HANDOFF_CONTRACT_VERSION = "phm-data-1.0.0_to_diagnosis-1.0.0"


def _payload_data(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the effective data object exactly as Diagnosis MCP's _unwrap() sees it."""
    if not payload:
        return {}
    if "data" in payload:
        data = payload.get("data")
        return dict(data) if isinstance(data, Mapping) else {}
    return dict(payload)


def _preserve_data_wrapper(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve Data MCP's wrapper while dropping success/data=None noise.

    phm-diagnosis-mcp 1.0.0 accepts both a flat waveform/trend object and the nested
    Data MCP object. Keeping the nested shape removes an unnecessary transformation
    step and makes the two MCPs' published contracts line up directly.
    """
    data = _payload_data(payload)
    if not data:
        return {}
    if "data" in payload:
        result: dict[str, Any] = {"data": data}
        decode = payload.get("decode")
        if isinstance(decode, Mapping):
            result["decode"] = dict(decode)
        return result
    return dict(payload)


def _usable_waveform_payload(payload: Mapping[str, Any]) -> bool:
    data = _payload_data(payload)
    value = data.get("values_base64") or data.get("float32_base64")
    sample_rate = data.get("sample_rate_hz") or data.get("fs_hz")
    try:
        fs = float(sample_rate)
    except (TypeError, ValueError):
        fs = 0.0
    return isinstance(value, str) and bool(value.strip()) and fs > 0


def _usable_trend_payload(payload: Mapping[str, Any]) -> bool:
    data = _payload_data(payload)
    if not data:
        return False
    binary = data.get("payload_base64")
    if isinstance(binary, str) and binary.strip():
        return True
    # An inline empty series is still a structurally valid trend query result.
    return isinstance(data.get("series"), list)


def _snapshot_parts(
    snapshot: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    data = _as_dict(snapshot.get("data"))
    return (
        _preserve_data_wrapper(_as_dict(data.get("waveform"))),
        _preserve_data_wrapper(_as_dict(data.get("feature_trend"))),
        _preserve_data_wrapper(_as_dict(data.get("temperature_trend"))),
    )


def _usable_payload(payload: Mapping[str, Any]) -> bool:
    if not payload:
        return False
    # Accept both Data MCP wrappers and already-normalized Diagnosis MCP payloads.
    if isinstance(payload.get("data"), Mapping):
        return payload.get("data") not in (None, {}, [])
    return any(
        payload.get(key) not in (None, "", [], {})
        for key in (
            "values_base64",
            "float32_base64",
            "series",
            "payload_base64",
            "point_id",
            "point_no",
        )
    )


def _speed_from_state(
    state: Mapping[str, Any],
    call_arguments: Mapping[str, Any],
    transient_payloads: Mapping[str, Any],
    *,
    current_run_only: bool = False,
) -> float | None:
    # Explicit caller value wins. For comprehensive diagnosis, otherwise only accept
    # an RPM extracted in the current worker run. Raw waveform payloads are intentionally
    # transient, so reusing a persisted RPM from another target/turn can silently bind
    # the wrong speed to a newly acquired waveform.
    candidates: list[Any] = [call_arguments.get("speed_rpm")]

    rpm_payload = _latest(transient_payloads, PHM_FEATURE_EXTRACT_RPM_TOOL_ID)
    if rpm_payload.get("supported") is not False:
        candidates.append(rpm_payload.get("rpm"))

    if not current_run_only:
        candidates.append(state.get("speed_rpm"))
        diagnosis = state.get("active_diagnosis_task")
        if isinstance(diagnosis, Mapping):
            candidates.extend(
                [
                    diagnosis.get("speed_rpm"),
                    diagnosis.get("rpm"),
                    _as_dict(diagnosis.get("speed")).get("rpm"),
                ]
            )

    for value in candidates:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return None

def _alarm_context(state: Mapping[str, Any], transient: Mapping[str, Any]) -> dict[str, Any]:
    diagnosis = _as_dict(state.get("active_diagnosis_task"))
    for key in ("alarm", "alarm_context"):
        item = diagnosis.get(key)
        if isinstance(item, Mapping) and item:
            return dict(item)

    latest_alarm = _latest(transient, PHM_QUERY_ALARM_RECORDS_TOOL_ID)
    data = latest_alarm.get("data")
    rows: list[Any] = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, Mapping):
        for key in ("rows", "records", "data"):
            candidate = data.get(key)
            if isinstance(candidate, list):
                rows = candidate
                break
    if rows and isinstance(rows[0], Mapping):
        row = rows[0]
        allowed = {
            "alarm_type",
            "alarm_type_name",
            "equip_no",
            "equip_name",
            "point_no",
            "model_no",
            "model_name",
            "warn_level",
            "warn_level_ch",
            "start_time",
            "latest_start_time",
            "latest_end_time",
        }
        return {key: row.get(key) for key in allowed if row.get(key) is not None}

    entity = _as_dict(state.get("selected_entity") or state.get("resolved_entity") or state.get("active_entity"))
    metadata = _as_dict(entity.get("metadata"))
    merged = {**metadata, **entity}
    result: dict[str, Any] = {}
    for source_key, target_key in (
        ("equip_no", "device_code"),
        ("device_code", "device_code"),
        ("equip_name", "device_name"),
        ("point_no", "point_no"),
    ):
        value = merged.get(source_key)
        if value not in (None, "") and target_key not in result:
            result[target_key] = value
    return result


def _dedupe_payloads(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # The same trend can arrive both through get_data_snapshot and a direct trend call.
    # Deduplicate by stable metadata rather than Python object identity.
    seen: set[tuple[Any, ...]] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        if not item:
            continue
        data = _payload_data(item)
        encoded = data.get("values_base64") or data.get("payload_base64") or ""
        series = data.get("series")
        marker = (
            data.get("point_no"),
            data.get("point_id"),
            data.get("resolved_time"),
            data.get("start_time"),
            data.get("end_time"),
            data.get("point_count"),
            len(encoded) if isinstance(encoded, str) else 0,
            len(series) if isinstance(series, list) else -1,
        )
        if marker in seen:
            continue
        seen.add(marker)
        result.append(item)
    return result


def build_phm_diagnosis_arguments(
    *,
    tool_id: str,
    state: Mapping[str, Any],
    call_arguments: Mapping[str, Any],
    transient_payloads: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    if tool_id == PHM_DIAGNOSIS_MODEL_ADMISSION_TOOL_ID:
        return {}, []

    speed_rpm = _speed_from_state(state, call_arguments, transient_payloads)

    if tool_id == PHM_DIAGNOSIS_ANALYZE_CHART_TOOL_ID:
        chart_type = str(call_arguments.get("chart_type") or "").strip()
        if not chart_type:
            return {}, ["chart_type"]
        arguments: dict[str, Any] = {"chart_type": chart_type}
        if speed_rpm is not None:
            arguments["speed_rpm"] = speed_rpm
        options = {
            key: call_arguments[key]
            for key in _ALLOWED_OPTIONS
            if call_arguments.get(key) not in (None, "")
        }
        if options:
            arguments["options"] = options

        snapshot = _latest(transient_payloads, PHM_GET_DATA_SNAPSHOT_TOOL_ID)
        snapshot_waveform, snapshot_feature, snapshot_temperature = _snapshot_parts(snapshot)
        missing: list[str] = []
        if chart_type in TREND_CHART_TYPES:
            if chart_type == "temperature_trend":
                trend = (
                    _latest(transient_payloads, PHM_GET_TEMPERATURE_TREND_TOOL_ID)
                    or snapshot_temperature
                )
            else:
                trend = _latest(transient_payloads, PHM_GET_FEATURE_TREND_TOOL_ID) or snapshot_feature
            if _usable_trend_payload(trend):
                arguments["trend"] = _preserve_data_wrapper(trend)
            else:
                missing.append("trend")
        else:
            waveform = _latest(transient_payloads, PHM_GET_WAVEFORM_TOOL_ID) or snapshot_waveform
            if _usable_waveform_payload(waveform):
                arguments["waveform"] = _preserve_data_wrapper(waveform)
            else:
                missing.append("waveform")
        if chart_type in ORDER_CHART_TYPES and speed_rpm is None:
            missing.append("speed_rpm")
        return arguments, missing

    if tool_id == PHM_DIAGNOSIS_POINT_TOOL_ID:
        # Single-point diagnosis keeps the R28 snapshot path, now mapped to the new
        # diagnose_point server contract.
        speed_rpm = _speed_from_state(
            state, call_arguments, transient_payloads, current_run_only=True
        )
        snapshot = _latest(transient_payloads, PHM_GET_DATA_SNAPSHOT_TOOL_ID)
        waveform, feature, snapshot_temperature = _snapshot_parts(snapshot)
        temperature = _latest(transient_payloads, PHM_GET_TEMPERATURE_TREND_TOOL_ID) or snapshot_temperature
        identity = resolve_phm_identity_fields(state)
        point_no = str(identity.get("wave_point_no") or identity.get("point_no") or "").strip()
        missing: list[str] = []
        if not point_no:
            missing.append("point_no")
        arguments: dict[str, Any] = {
            "point_no": point_no,
            "feature_trends": [feature] if _usable_trend_payload(feature) else [],
            "temperature_trends": [temperature] if _usable_trend_payload(temperature) else [],
            "use_model": bool(call_arguments.get("use_model", True)),
        }
        if _usable_waveform_payload(waveform):
            arguments["waveform"] = waveform
        if not arguments.get("waveform") and not arguments["feature_trends"] and not arguments["temperature_trends"]:
            missing.append("waveform 或 trend 数据")
        alarm = _alarm_context(state, transient_payloads)
        if alarm:
            arguments["alarm"] = alarm
        if speed_rpm is not None:
            arguments["speed_rpm"] = speed_rpm
        return arguments, missing

    if tool_id == PHM_DIAGNOSIS_DEVICE_TOOL_ID:
        device_payload = _latest(transient_payloads, PHM_GET_DEVICE_DATA_TOOL_ID)
        data = _payload_data(device_payload)
        device_code = str(data.get("device_code") or resolve_phm_identity_fields(state).get("device_code") or "").strip()
        waveform_decode = _as_dict(device_payload.get("waveform_decode"))
        groups: dict[str, dict[str, Any]] = {}

        def group_for(code: str) -> dict[str, Any]:
            derived = derive_phm_point_codes(code)
            key = str(derived.get("wave_point_no") or raw_measurement_point_code(code) or code)
            return groups.setdefault(key, {"point_no": key, "feature_trends": [], "temperature_trends": []})

        for item in data.get("waveforms") or []:
            if not isinstance(item, Mapping):
                continue
            point_no = str(item.get("point_no") or "").strip()
            if not point_no:
                continue
            wrapper: dict[str, Any] = {"data": dict(item)}
            if waveform_decode:
                wrapper["decode"] = waveform_decode
            if _usable_waveform_payload(wrapper):
                group_for(point_no)["waveform"] = wrapper

        for item in data.get("feature_trends") or []:
            if not isinstance(item, Mapping):
                continue
            point_id = str(item.get("point_id") or "").strip()
            if not point_id:
                continue
            group = group_for(point_id)
            raw = raw_measurement_point_code(point_id) or point_id
            if raw.endswith("T"):
                group["temperature_trends"].append(dict(item))
            else:
                group["feature_trends"].append(dict(item))

        points = [
            item for item in groups.values()
            if item.get("waveform") or item.get("feature_trends") or item.get("temperature_trends")
        ]
        missing: list[str] = []
        if not device_code:
            missing.append("device_code/equip_no")
        if not points:
            missing.append("device points")
        arguments = {
            "device_code": device_code,
            "points": points,
            "use_model": bool(call_arguments.get("use_model", True)),
        }
        # Device MCP accepts one optional RPM. Only forward a current-run reliable RPM;
        # never reuse stale speed from a previous entity/turn.
        speed_rpm = _speed_from_state(state, call_arguments, transient_payloads, current_run_only=True)
        if speed_rpm is not None:
            arguments["speed_rpm"] = speed_rpm
        return arguments, missing

    return {
        key: value
        for key, value in call_arguments.items()
        if key not in _CONTROL_ARGUMENTS and value not in (None, "", [], {})
    }, []


def compact_diagnosis_result(value: Any) -> Any:
    """Keep diagnosis findings/metrics while avoiding large chart arrays in persistence."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key == "chart" and isinstance(item, Mapping):
                traces = item.get("traces")
                result[key] = {
                    "traces_omitted": True,
                    "trace_count": len(traces) if isinstance(traces, list) else 0,
                    "layout": compact_diagnosis_result(item.get("layout") or {}),
                }
            elif key == "charts" and isinstance(item, list):
                compacted = []
                for chart in item:
                    if not isinstance(chart, Mapping):
                        continue
                    compacted.append(
                        {
                            k: compact_diagnosis_result(v)
                            for k, v in chart.items()
                            if k != "chart"
                        }
                    )
                result[key] = compacted
            else:
                result[key] = compact_diagnosis_result(item)
        return result
    if isinstance(value, list):
        # Metrics/findings are typically small. Very large numeric arrays are clipped.
        if len(value) > 500:
            return {
                "omitted": True,
                "reason": "large_numeric_array_not_persisted",
                "item_count": len(value),
            }
        return [compact_diagnosis_result(item) for item in value]
    return value
