from __future__ import annotations

from typing import Any
from pydantic import Field, model_validator
from .common import StrictModel


class QuerySpaceTreeRequest(StrictModel):
    root_space_id: str = Field(min_length=1)
    max_depth: int = Field(default=10, ge=1, le=100)
    include_devices: bool = False
    include_points: bool = False

    @model_validator(mode="after")
    def validate_points(self) -> "QuerySpaceTreeRequest":
        if self.include_points and not self.include_devices:
            raise ValueError("include_points=true requires include_devices=true")
        return self


class QuerySpaceChildrenRequest(StrictModel):
    space_id: str = Field(min_length=1)
    child_type: str | None = None
    recursive: bool = False


class SpaceNode(StrictModel):
    node_type: str = "space"
    space_id: str
    parent_space_id: str | None = None
    space_name: str
    space_type: str | None = None
    space_no: str | None = None
    depth: int = 0
    path: str | None = None
    space_link: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
