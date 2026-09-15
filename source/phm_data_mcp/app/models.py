from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class TrendSample:
    time: str
    value: float
    quality: Any = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"time": self.time, "value": self.value}
        if self.quality is not None:
            data["quality"] = self.quality
        return data


@dataclass(slots=True)
class TrendSeries:
    kpi_id: str
    samples: list[TrendSample] = field(default_factory=list)
    name: str | None = None
    unit: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kpi_id": self.kpi_id, "samples": [x.to_dict() for x in self.samples]}
        if self.name:
            data["name"] = self.name
        if self.unit:
            data["unit"] = self.unit
        return data


@dataclass(slots=True)
class AlarmQuerySpec:
    query: str = ""
    alarm_types: list[str] = field(default_factory=lambda: ["threshold", "trend", "diagnosis", "ai"])
    equip_no: str | None = None
    equip_name: str | None = None
    equip_name_keyword: str | None = None
    point_no: str | None = None
    model_no: str | None = None
    space_link: str | None = None
    space_name: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    target_time: datetime | None = None
    time_mode: str = "default"
    alarm_state: str = "active"
    end_exclusive: bool = False
    metric: str = "detail"
    group_by: str | None = None
    limit: int = 20
