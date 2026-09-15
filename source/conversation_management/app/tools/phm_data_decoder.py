from __future__ import annotations

import base64
import gzip
import json
import math
import statistics
import struct
from collections.abc import Mapping
from typing import Any

_BASE64_KEYS = {"values_base64", "payload_base64"}


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _payload_data(value: Mapping[str, Any]) -> dict[str, Any]:
    nested = value.get("data")
    return _as_dict(nested) if isinstance(nested, Mapping) else dict(value)


def _decode_meta(value: Mapping[str, Any], data: Mapping[str, Any]) -> dict[str, Any]:
    decode = value.get("decode")
    if isinstance(decode, Mapping):
        return dict(decode)
    decode = data.get("decode")
    if isinstance(decode, Mapping):
        return dict(decode)
    return {}


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _sample_evenly(items: list[Any], limit: int) -> list[Any]:
    if limit <= 0 or len(items) <= limit:
        return items
    if limit == 1:
        return [items[-1]]
    indexes = sorted({round(i * (len(items) - 1) / (limit - 1)) for i in range(limit)})
    return [items[index] for index in indexes]


def decode_waveform_view(value: Mapping[str, Any], *, preview_points: int = 96) -> dict[str, Any]:
    data = _payload_data(value)
    encoded = data.get("values_base64") or data.get("float32_base64")
    if not isinstance(encoded, str) or not encoded.strip():
        return {"decoded": False, "reason": "waveform_payload_missing"}

    decode = _decode_meta(value, data)
    serialization = str(decode.get("serialization") or "float32_array")
    byte_order = str(decode.get("byte_order") or "little")
    if serialization != "float32_array" or byte_order != "little":
        return {
            "decoded": False,
            "reason": "unsupported_waveform_encoding",
            "serialization": serialization,
            "byte_order": byte_order,
        }
    try:
        raw = base64.b64decode(encoded, validate=False)
    except Exception as exc:
        return {"decoded": False, "reason": "invalid_base64", "error": str(exc)}
    if len(raw) % 4:
        return {"decoded": False, "reason": "invalid_float32_byte_length", "byte_length": len(raw)}

    values = [item[0] for item in struct.iter_unpack("<f", raw)]
    finite = [float(v) for v in values if math.isfinite(v)]
    if not finite:
        return {"decoded": True, "sample_count": len(values), "values_preview": []}

    mean = statistics.fmean(finite)
    rms = math.sqrt(statistics.fmean(v * v for v in finite))
    std = statistics.pstdev(finite) if len(finite) > 1 else 0.0
    preview = _sample_evenly(finite, preview_points)
    return {
        "decoded": True,
        "kind": "waveform",
        "point_no": data.get("point_no"),
        "resolved_time": data.get("resolved_time"),
        "sample_rate_hz": data.get("sample_rate_hz") or data.get("fs_hz"),
        "sample_count": len(values),
        "statistics": {
            "min": min(finite),
            "max": max(finite),
            "mean": mean,
            "std": std,
            "rms": rms,
        },
        "values_preview": preview,
        "preview_count": len(preview),
        "preview_sampling": "evenly_sampled" if len(finite) > len(preview) else "full",
    }


def _decode_trend_series(value: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], str | None]:
    data = _payload_data(value)
    inline = data.get("series")
    if isinstance(inline, list):
        return [dict(item) if isinstance(item, Mapping) else item for item in inline], data, None

    encoded = data.get("payload_base64")
    if not isinstance(encoded, str) or not encoded.strip():
        return [], data, "trend_payload_missing"
    decode = _decode_meta(value, data)
    serialization = str(decode.get("serialization") or "")
    compression = str(decode.get("compression") or "")
    if serialization != "json" or compression != "gzip":
        return [], data, "unsupported_trend_encoding"
    try:
        raw = gzip.decompress(base64.b64decode(encoded, validate=False))
        decoded = json.loads(raw.decode(str(decode.get("charset") or "utf-8")))
    except Exception as exc:
        return [], data, f"trend_decode_failed:{exc}"
    if not isinstance(decoded, list):
        return [], data, "decoded_trend_not_list"
    return [dict(item) if isinstance(item, Mapping) else item for item in decoded], data, None


def decode_trend_view(value: Mapping[str, Any], *, max_samples_per_series: int = 500) -> dict[str, Any]:
    series, data, error = _decode_trend_series(value)
    if error:
        return {"decoded": False, "reason": error, "point_id": data.get("point_id")}

    public_series: list[dict[str, Any]] = []
    all_values: list[float] = []
    total_samples = 0
    for item in series:
        if not isinstance(item, Mapping):
            continue
        samples = item.get("samples")
        sample_list = [dict(x) for x in samples if isinstance(x, Mapping)] if isinstance(samples, list) else []
        total_samples += len(sample_list)
        values = [n for n in (_finite_number(x.get("value")) for x in sample_list) if n is not None]
        all_values.extend(values)
        shown = _sample_evenly(sample_list, max_samples_per_series)
        row: dict[str, Any] = {
            "kpi_id": item.get("kpi_id"),
            "name": item.get("name"),
            "sample_count": len(sample_list),
            "samples": shown,
            "samples_truncated": len(shown) < len(sample_list),
        }
        if values:
            row["statistics"] = {
                "min": min(values),
                "max": max(values),
                "avg": statistics.fmean(values),
                "first": values[0],
                "last": values[-1],
            }
        public_series.append(row)

    result: dict[str, Any] = {
        "decoded": True,
        "kind": "trend",
        "point_id": data.get("point_id"),
        "start_time": data.get("start_time"),
        "end_time": data.get("end_time"),
        "point_count": data.get("point_count") or total_samples,
        "series": public_series,
    }
    if all_values:
        result["overall_statistics"] = {
            "min": min(all_values),
            "max": max(all_values),
            "avg": statistics.fmean(all_values),
        }
    return result


def decode_device_data_view(value: Mapping[str, Any]) -> dict[str, Any]:
    data = _payload_data(value)
    waveform_decode = _as_dict(value.get("waveform_decode"))
    waveform_views: list[dict[str, Any]] = []
    for item in data.get("waveforms") or []:
        if not isinstance(item, Mapping):
            continue
        wrapper = {"data": dict(item), "decode": waveform_decode}
        waveform_views.append(decode_waveform_view(wrapper, preview_points=48))

    trend_views: list[dict[str, Any]] = []
    for item in data.get("feature_trends") or []:
        if isinstance(item, Mapping):
            trend_views.append(decode_trend_view(item, max_samples_per_series=120))

    return {
        "decoded": True,
        "kind": "device_data",
        "device_code": data.get("device_code"),
        "anchor_time": data.get("anchor_time"),
        "waveform_count": len(waveform_views),
        "trend_count": len(trend_views),
        "waveforms": waveform_views,
        "feature_trends": trend_views,
        "errors": value.get("errors") or [],
    }


def decode_phm_data_for_public(tool_id: str, payload: Any) -> Any:
    """Return a model/persistence-safe decoded view while keeping raw payload untouched.

    The raw Base64 remains in GraphRuntime.transient_tool_payloads for Feature/Diagnosis
    MCP hand-off. This function is only for public/model-visible evidence.
    """
    if not isinstance(payload, Mapping):
        return payload
    if tool_id.endswith("get_waveform"):
        return decode_waveform_view(payload)
    if tool_id.endswith("get_feature_trend") or tool_id.endswith("get_temperature_trend"):
        return decode_trend_view(payload)
    if tool_id.endswith("get_device_data"):
        return decode_device_data_view(payload)
    if tool_id.endswith("get_data_snapshot"):
        data = _payload_data(payload)
        view: dict[str, Any] = {
            "decoded": True,
            "kind": "snapshot",
            "anchor_time": data.get("anchor_time"),
            "warnings": payload.get("warnings") or [],
        }
        waveform = data.get("waveform")
        if isinstance(waveform, Mapping):
            view["waveform"] = decode_waveform_view(waveform)
        trend = data.get("feature_trend")
        if isinstance(trend, Mapping):
            view["feature_trend"] = decode_trend_view(trend)
        temperature = data.get("temperature_trend")
        if isinstance(temperature, Mapping):
            view["temperature_trend"] = decode_trend_view(temperature)
        return view
    return payload
