from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class TopicManifest(BaseModel):
    topic_id: UUID
    slot: Literal["CURRENT", "PREVIOUS_1", "PREVIOUS_2", "COLD"] | None = None
    title: str
    topic_summary: str = ""
    subject: dict[str, Any] = Field(default_factory=dict)
    scope: dict[str, Any] = Field(default_factory=dict)
    goal_summary: str = ""
    evidence_types: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None


class ContextResolutionDecision(BaseModel):
    action: Literal[
        "CONTINUE_CURRENT", "RESUME_PREVIOUS_1", "RESUME_PREVIOUS_2",
        "RESUME_COLD_TOPIC", "NEW_TOPIC",
    ] = "NEW_TOPIC"
    topic_id: UUID | None = None
    reason: str = Field(default="", max_length=180)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_topic(self):
        if self.action != "NEW_TOPIC" and self.topic_id is None:
            raise ValueError("resume/continue context action requires topic_id")
        return self
