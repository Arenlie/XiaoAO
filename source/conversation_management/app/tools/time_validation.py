from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo


_ALLOWED_ALARM_TIME_MODES = {"default", "latest", "nearest", "latest_before", "range"}


def _parse_absolute_time(value: Any, *, timezone_name: str, field_name: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name}不能为空")
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name}必须是ISO 8601绝对时间，实际：{text}") from exc
    tz = ZoneInfo(timezone_name)
    if dt.tzinfo is None:
        # Compatibility normalization only: the model is instructed to emit an offset,
        # but a timezone-less absolute timestamp is interpreted in the configured
        # business timezone instead of reparsing the user's natural-language text.
        dt = dt.replace(tzinfo=tz)
    else:
        dt = dt.astimezone(tz)
    return dt


def validate_absolute_range_arguments(
    arguments: Mapping[str, Any],
    *,
    timezone_name: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """Validate/normalize explicit absolute start/end timestamps only."""
    result = dict(arguments)
    parsed: dict[str, datetime] = {}
    for field in ("start_time", "end_time"):
        value = result.get(field)
        if value in (None, ""):
            result.pop(field, None)
            continue
        dt = _parse_absolute_time(value, timezone_name=timezone_name, field_name=field)
        parsed[field] = dt
        result[field] = dt.isoformat(timespec="seconds")
    start = parsed.get("start_time")
    end = parsed.get("end_time")
    if start and end and start > end:
        raise ValueError("start_time不能晚于end_time")
    return result


def validate_alarm_time_arguments(
    arguments: Mapping[str, Any],
    *,
    timezone_name: str = "Asia/Shanghai",
) -> dict[str, Any]:
    """Validate/normalize already-structured alarm time arguments.

    This function deliberately does not inspect the user's natural-language query and
    contains no Chinese temporal-expression rules. Time semantics are owned by the LLM;
    the backend only validates the absolute timestamps it produced.
    """

    result = dict(arguments)
    mode = str(result.get("time_mode") or "default").strip().lower()
    if mode not in _ALLOWED_ALARM_TIME_MODES:
        raise ValueError("time_mode只支持default、latest、nearest、latest_before、range")

    parsed: dict[str, datetime] = {}
    for field in ("start_time", "end_time", "target_time"):
        value = result.get(field)
        if value in (None, ""):
            result.pop(field, None)
            continue
        dt = _parse_absolute_time(value, timezone_name=timezone_name, field_name=field)
        parsed[field] = dt
        result[field] = dt.isoformat(timespec="seconds")

    start = parsed.get("start_time")
    end = parsed.get("end_time")
    target = parsed.get("target_time")
    if start and end and start > end:
        raise ValueError("start_time不能晚于end_time")

    # Absolute boundaries imply a range query even if the model left time_mode at the
    # generic default. This is argument normalization, not natural-language parsing.
    if start or end:
        mode = "range"
    if mode == "range" and not (start or end):
        raise ValueError("time_mode=range时必须提供start_time或end_time")
    if mode in {"nearest", "latest_before"} and target is None:
        raise ValueError(f"time_mode={mode}时必须提供target_time")

    result["time_mode"] = mode
    return result
