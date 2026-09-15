from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SensorQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    equip_num: str | None = Field(default=None, min_length=1, max_length=256)
    point_num: str | None = Field(default=None, min_length=1, max_length=256)
    fault_type: str | None = Field(default=None, min_length=1, max_length=128)
    fault_status: Literal["PENDING_CONFIRMATION", "PENDING_REPAIR", "REPAIR_COMPLETED", "AUTO_RECOVERED", "DATA_INTERRUPTED"] | None = None
    start_time_from: str | None = None
    start_time_to: str | None = None
    end_time_from: str | None = None
    end_time_to: str | None = None
    limit: int = Field(default=50, ge=1, le=200)
    include_analysis: bool = True
    refresh: bool = False

    @model_validator(mode="after")
    def check_scope(self):
        if self.point_num and not self.equip_num:
            raise ValueError("指定测点时必须同时提供真实设备编码，避免同名或同号测点串用")
        for key in ("start_time_from", "start_time_to", "end_time_from", "end_time_to"):
            raw = getattr(self, key)
            if raw is not None:
                try:
                    datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError("时间参数必须是可解析的 ISO 8601 时间") from exc
        return self


class MonitoredPointsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    equip_num: str | None = Field(default=None, min_length=1, max_length=256)
    equip_name: str | None = Field(default=None, min_length=1, max_length=256)
    point_num: str | None = Field(default=None, min_length=1, max_length=256)
    point_name: str | None = Field(default=None, min_length=1, max_length=256)
    monitor_status: Literal["ONLINE", "OFFLINE", "SUSPENDED"] | None = None
    waveform_enabled: bool | None = None
    feature_kind: Literal["bias", "velocity", "temperature"] | None = None
    limit: int = Field(default=1000, ge=1, le=1000)
    refresh: bool = False

    @field_validator("monitor_status", mode="before")
    @classmethod
    def normalize_status(cls, value):
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("feature_kind", mode="before")
    @classmethod
    def normalize_kind(cls, value):
        return value.strip().lower() if isinstance(value, str) else value
