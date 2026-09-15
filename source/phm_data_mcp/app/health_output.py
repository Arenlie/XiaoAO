from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Asia/Shanghai")
_TIMESTAMP_KEYS = {"ts", "timestamp"}
_GRADE_LABELS = {
    1: "重点关注",
    2: "早期关注",
    3: "良好",
    4: "优秀",
    5: "离线",
}


def format_health_output(result: dict[str, Any]) -> dict[str, Any]:
    """格式化健康度结果：转换时间戳、映射健康等级。"""
    if not isinstance(result, dict):
        return result

    data = result.get("data")
    if data is not None:
        result["data"] = _convert(data)
    return result


def _convert(value: Any, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {name: _convert(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_convert(item) for item in value]
    if key == "grade" and isinstance(value, int) and not isinstance(value, bool):
        return _GRADE_LABELS.get(value, value)
    if key and _is_timestamp_key(key) and _is_number(value):
        return _format_timestamp(value)
    return value


def _is_timestamp_key(key: str) -> bool:
    normalized = key.lower()
    return (
        normalized in _TIMESTAMP_KEYS
        or normalized.endswith("_ts")
        or normalized.endswith("_timestamp")
    )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _format_timestamp(value: int | float) -> str:
    # 现场健康度 ts 为毫秒时间戳；同时兼容秒级 Unix timestamp。
    seconds = float(value) / 1000.0 if abs(float(value)) >= 100_000_000_000 else float(value)
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(LOCAL_TZ)

    if dt.microsecond:
        return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return dt.strftime("%Y-%m-%d %H:%M:%S")
