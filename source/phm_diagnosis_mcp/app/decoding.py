from __future__ import annotations

import base64
import gzip
import json
from typing import Any

import numpy as np


def _unwrap(value: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Accept either a Data MCP result or its nested waveform/trend object."""
    if "data" in value and isinstance(value.get("data"), dict):
        return value["data"], value.get("decode") or {}
    return value, value.get("decode") or {}


def decode_waveform(value: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any]]:
    data, decode = _unwrap(value)
    encoded = data.get("values_base64")
    if not encoded:
        raise ValueError("waveform.values_base64不能为空")

    serialization = decode.get("serialization") or "float32_array"
    byte_order = decode.get("byte_order") or "little"
    if serialization != "float32_array":
        raise ValueError(f"不支持的波形serialization: {serialization}")
    if byte_order != "little":
        raise ValueError(f"不支持的波形byte_order: {byte_order}")

    raw = base64.b64decode(encoded)
    if len(raw) % 4 != 0:
        raise ValueError("波形字节数不是float32字节数4的整数倍")
    values = np.frombuffer(raw, dtype="<f4").astype(float, copy=False)
    meta = {
        "point_no": data.get("point_no"),
        "resolved_time": data.get("resolved_time"),
        "sample_rate_hz": float(data.get("sample_rate_hz") or 0.0),
    }
    if meta["sample_rate_hz"] <= 0:
        raise ValueError("waveform.sample_rate_hz必须大于0")
    return values, meta


def decode_trend(value: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data, decode = _unwrap(value)
    if data.get("series") is not None:
        series = data.get("series") or []
    elif data.get("payload_base64"):
        if decode.get("serialization") != "json" or decode.get("compression") != "gzip":
            raise ValueError("当前只支持gzip JSON格式的二进制趋势数据")
        raw = gzip.decompress(base64.b64decode(data["payload_base64"]))
        series = json.loads(raw.decode(decode.get("charset") or "utf-8"))
    else:
        series = []

    if not isinstance(series, list):
        raise ValueError("趋势数据series必须是数组")
    meta = {
        "point_id": data.get("point_id"),
        "start_time": data.get("start_time"),
        "end_time": data.get("end_time"),
    }
    return series, meta
