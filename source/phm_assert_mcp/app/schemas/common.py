from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ToolErrorResponse(StrictModel):
    success: bool = False
    status: str
    message: str
    error: dict[str, Any] = Field(default_factory=dict)


class EntityView(StrictModel):
    entity_type: str
    space_id: str | None = None
    space_name: str | None = None
    space_path: str | None = None
    space_link: str | None = None
    space_type: str | None = None
    space_no: str | None = None
    equip_id: str | None = None
    equip_no: str | None = None
    equip_name: str | None = None
    point_id: str | None = None
    point_no: str | None = None
    point_name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
