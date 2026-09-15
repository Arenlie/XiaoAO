from __future__ import annotations

from typing import Any, Mapping

from app.tools.phm_data_mcp import PHM_GET_DATA_SNAPSHOT_TOOL_ID, PHM_GET_WAVEFORM_TOOL_ID
from app.tools.phm_feature_mcp import (
    PHM_FEATURE_EXTRACT_RPM_TOOL_ID,
    PHM_FEATURE_EXTRACT_VIBRATION_TOOL_ID,
)

_CONTROL_ARGUMENTS = {"objective", "required_entity_level"}


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _latest(transient: Mapping[str, Any], tool_id: str) -> dict[str, Any]:
    return _as_dict(transient.get(f"latest:{tool_id}"))


def _entity_payload(state: Mapping[str, Any]) -> dict[str, Any]:
    entity = _as_dict(
        state.get("selected_entity")
        or state.get("resolved_entity")
        or state.get("active_entity")
    )
    metadata = _as_dict(entity.get("metadata"))
    return {**metadata, **entity}


def _waveform_wrapper(transient: Mapping[str, Any]) -> dict[str, Any]:
    direct = _latest(transient, PHM_GET_WAVEFORM_TOOL_ID)
    if direct:
        return direct
    snapshot = _latest(transient, PHM_GET_DATA_SNAPSHOT_TOOL_ID)
    snapshot_data = _as_dict(snapshot.get("data"))
    return _as_dict(snapshot_data.get("waveform"))


def _waveform_data(wrapper: Mapping[str, Any]) -> dict[str, Any]:
    # Data MCP get_waveform returns {success,data,decode}; snapshot.waveform returns
    # the same inner waveform object. Keep both forms compatible.
    data = wrapper.get("data")
    if isinstance(data, Mapping):
        return dict(data)
    return dict(wrapper)


def _device_info(state: Mapping[str, Any], waveform: Mapping[str, Any]) -> dict[str, Any]:
    entity = _entity_payload(state)
    result: dict[str, Any] = {}
    mappings = (
        ("equip_no", "device_code"),
        ("device_code", "device_code"),
        ("equip_name", "device_name"),
        ("device_name", "device_name"),
        ("point_no", "point_code"),
        ("point_code", "point_code"),
        ("point_name", "point_name"),
        ("space_name", "location"),
        ("space_path", "location"),
    )
    for source, target in mappings:
        value = entity.get(source)
        if value not in (None, "") and target not in result:
            result[target] = value
    point_no = waveform.get("point_no")
    if point_no not in (None, ""):
        result["point_code"] = point_no
    return result


def _safe_frequency_bands(
    raw: Any,
    *,
    nyquist: float,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(raw, list):
        return [], []
    result: list[dict[str, Any]] = []
    issues: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            issues.append(f"frequency_bands[{index}]")
            continue
        try:
            low = float(item.get("low_hz"))
            high = float(item.get("high_hz"))
        except (TypeError, ValueError):
            issues.append(f"frequency_bands[{index}]")
            continue
        name = str(item.get("name") or f"band_{index + 1}").strip()
        if low < 0 or high <= low or high > nyquist:
            issues.append(f"frequency_bands[{index}]超出0~Nyquist({nyquist:g}Hz)")
            continue
        result.append({"name": name, "low_hz": low, "high_hz": high})
    return result, issues


def build_phm_feature_arguments(
    *,
    tool_id: str,
    state: Mapping[str, Any],
    call_arguments: Mapping[str, Any],
    transient_payloads: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    wrapper = _waveform_wrapper(transient_payloads)
    waveform = _waveform_data(wrapper)
    missing: list[str] = []
    try:
        fs_hz = float(waveform.get("sample_rate_hz") or waveform.get("fs_hz"))
    except (TypeError, ValueError):
        fs_hz = 0.0
    base64_value = waveform.get("values_base64") or waveform.get("float32_base64")
    if fs_hz <= 0:
        missing.append("waveform.sample_rate_hz")
    if not isinstance(base64_value, str) or not base64_value:
        missing.append("waveform.values_base64")
    if missing:
        return {}, missing

    signal_type = str(call_arguments.get("signal_type") or "acceleration").strip().lower()
    if signal_type not in {"acceleration", "velocity", "displacement"}:
        signal_type = "acceleration"
    unit = call_arguments.get("unit")
    if unit in (None, ""):
        entity = _entity_payload(state)
        unit = entity.get("unit") or entity.get("measurement_unit")

    signal_input: dict[str, Any] = {
        "fs_hz": fs_hz,
        "signal_type": signal_type,
        "data": {"float32_base64": base64_value},
    }
    if unit not in (None, ""):
        signal_input["unit"] = str(unit)
    device_info = _device_info(state, waveform)
    if device_info:
        signal_input["device_info"] = device_info

    if tool_id == PHM_FEATURE_EXTRACT_VIBRATION_TOOL_ID:
        feature_set = str(call_arguments.get("feature_set") or "standard")
        if feature_set not in {"basic", "standard", "bearing", "full"}:
            feature_set = "standard"
        options: dict[str, Any] = {
            "feature_set": feature_set,
            "detrend": bool(call_arguments.get("detrend", True)),
            "window": "hann",
        }
        nyquist = fs_hz / 2.0
        frequency_bands, issues = _safe_frequency_bands(
            call_arguments.get("frequency_bands"),
            nyquist=nyquist,
        )
        missing.extend(issues)
        if frequency_bands:
            options["frequency_bands"] = frequency_bands
        envelope = call_arguments.get("envelope_band")
        if isinstance(envelope, Mapping):
            try:
                low = float(envelope.get("low_hz"))
                high = float(envelope.get("high_hz"))
                order = int(envelope.get("filter_order", 4))
            except (TypeError, ValueError):
                missing.append("envelope_band")
            else:
                if low <= 0 or high <= low or high >= nyquist or not 1 <= order <= 10:
                    missing.append(f"envelope_band必须满足0<low<high<Nyquist({nyquist:g}Hz)")
                else:
                    options["envelope_band"] = {
                        "low_hz": low,
                        "high_hz": high,
                        "filter_order": order,
                    }
        return {"signal_input": signal_input, "options": options}, missing

    if tool_id == PHM_FEATURE_EXTRACT_RPM_TOOL_ID:
        payload = {"float32_base64": base64_value}
        arguments: dict[str, Any] = {
            "fs_hz": fs_hz,
            "device_info": device_info or None,
            "request_id": str(state.get("task_id") or "") or None,
        }
        if signal_type == "velocity":
            arguments["velocity"] = payload
        else:
            # Data MCP vibration waveform is acceleration by default. The Feature MCP
            # may derive velocity internally when only acceleration is provided.
            arguments["acceleration"] = payload
        if "use_llm_judge" in call_arguments and call_arguments.get("use_llm_judge") is not None:
            arguments["use_llm_judge"] = bool(call_arguments.get("use_llm_judge"))
        return {k: v for k, v in arguments.items() if v is not None}, []

    return {
        key: value
        for key, value in call_arguments.items()
        if key not in _CONTROL_ARGUMENTS and value not in (None, "", [], {})
    }, []


def extract_supported_rpm(payload: Mapping[str, Any]) -> float | None:
    if payload.get("supported") is False:
        return None
    try:
        rpm = float(payload.get("rpm"))
    except (TypeError, ValueError):
        return None
    return rpm if rpm > 0 else None
