from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class RuntimeClockSnapshot:
    timezone_name: str
    now: datetime

    @property
    def iso(self) -> str:
        return self.now.isoformat(timespec="seconds")

    @property
    def utc_offset(self) -> str:
        offset = self.now.utcoffset()
        if offset is None:
            return "+00:00"
        seconds = int(offset.total_seconds())
        sign = "+" if seconds >= 0 else "-"
        seconds = abs(seconds)
        hours, remainder = divmod(seconds, 3600)
        minutes = remainder // 60
        return f"{sign}{hours:02d}:{minutes:02d}"

    def as_context(self) -> dict[str, str]:
        return {
            "source": "server_runtime_clock",
            "current_time": self.iso,
            "timezone": self.timezone_name,
            "utc_offset": self.utc_offset,
        }

    def prompt_block(self) -> str:
        return (
            "【权威运行时钟】\n"
            f"当前真实时间：{self.iso}\n"
            f"当前时区：{self.timezone_name}（UTC{self.utc_offset}）\n"
            "这是由后端在本次模型调用前实时读取的服务器时钟，是理解“现在、今天、昨天、"
            "最近一周、本周、上个月、某日到现在”等相对时间表达的唯一时间基准。\n"
            "禁止从历史对话、知识库、设备数据时间戳或模型知识中推断当前日期/时间。"
        )


def runtime_clock_snapshot(timezone_name: str) -> RuntimeClockSnapshot:
    tz = ZoneInfo(timezone_name)
    return RuntimeClockSnapshot(timezone_name=timezone_name, now=datetime.now(tz))
