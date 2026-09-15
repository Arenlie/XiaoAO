from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.agents.contracts import AgentDescriptor


class AgentRuntimeUpdate(BaseModel):
    enabled: bool | None = None
    routing_enabled: bool | None = None
    execution_enabled: bool | None = None
    maintenance_message: str | None = Field(default=None, max_length=1000)
    priority_override: int | None = Field(default=None, ge=0, le=10000)
    timeout_seconds_override: int | None = Field(default=None, ge=1, le=3600)
    config_json: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_explicit_nulls(self):
        for field in ("enabled", "routing_enabled", "execution_enabled"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} 不能为 null")
        return self


class AgentDescriptorView(BaseModel):
    descriptor: AgentDescriptor
    source: str = "catalog"


class AgentHealthCheckResponse(BaseModel):
    agent_id: str
    status: str
    message: str
    checked_at: str
    details: dict[str, Any] = Field(default_factory=dict)
