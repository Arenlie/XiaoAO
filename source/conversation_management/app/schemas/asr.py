from __future__ import annotations

from pydantic import BaseModel, Field


class AsrTranscriptionResponse(BaseModel):
    text: str
    language: str | None = None
    emotion: str | None = None
    duration_seconds: float | None = None
    provider: str
    model: str
    upstream_request_id: str | None = None
    filename: str | None = None


class AsrStatusResponse(BaseModel):
    enabled: bool
    provider: str
    model: str
    max_file_bytes: int = Field(ge=1)
    default_language: str | None = None
    realtime_enabled: bool = False
    realtime_provider: str | None = None
    realtime_model: str | None = None
    realtime_websocket_path: str | None = None
