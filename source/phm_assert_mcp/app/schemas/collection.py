from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import StrictModel


class QueryScopeCollectionRequest(StrictModel):
    """Return a deterministic collection below one already-resolved space.

    The caller supplies structured hierarchy intent.  This contract deliberately
    contains no natural-language rules: language understanding belongs to the model,
    while this request only carries the resolved root and requested target level/type.
    """

    root_space_id: str = Field(min_length=1)
    target_entity_level: Literal["space", "equipment", "point"]
    target_space_type: str | None = Field(default=None, max_length=128)
    target_equipment_type: str | None = Field(default=None, max_length=128)
    semantic_filters: list[str] = Field(default_factory=list, max_length=8)
    output_mode: Literal["list", "count"] = "list"
    recursive: bool = True
    # Logical collection limit, not a single SQL LIMIT. Asset MCP pages internally.
    limit: int = Field(default=50000, ge=1, le=100000)
