from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.config import get_settings


def timezone() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def now() -> datetime:
    return datetime.now(timezone())


def parse_time(value: Any | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone())
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            dt = None
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S"):
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            if dt is None:
                raise ValueError(f"无法解析时间：{value}")
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone())
    return dt.astimezone(timezone())


def to_iso(value: Any | None) -> str | None:
    dt = parse_time(value)
    return dt.isoformat(timespec="seconds") if dt else None


def to_millis(value: Any) -> int:
    dt = parse_time(value)
    if dt is None:
        raise ValueError("时间不能为空")
    return int(dt.timestamp() * 1000)


def default_trend_range(end_time: Any | None, days: int) -> tuple[datetime, datetime]:
    end = parse_time(end_time) or now()
    return end - timedelta(days=max(1, days)), end
