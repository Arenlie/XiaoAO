from __future__ import annotations

from pydantic import BaseModel, Field


class ModelProfile(BaseModel):
    profile_id: str
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    supports_text: bool = True
    supports_vision: bool = False
    supports_files: bool = False
    supports_tool_calling: bool = False
    supports_structured_output: bool = False
    supports_reasoning: bool = False
    speed_rank: int = 50
    intelligence_rank: int = 50
    enabled: bool = True
    metadata: dict = Field(default_factory=dict)
