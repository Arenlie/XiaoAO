from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class SupervisorVerdictType(StrEnum):
    ANSWERABLE = "ANSWERABLE"
    NEED_MORE_EVIDENCE = "NEED_MORE_EVIDENCE"
    NEED_USER_INPUT = "NEED_USER_INPUT"
    CANNOT_ANSWER = "CANNOT_ANSWER"


class AgentCall(BaseModel):
    call_id: str
    call_type: Literal["agent", "tool"] = "agent"
    agent_id: str = ""
    tool_id: str = ""
    objective: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    workflow_id: str | None = None
    workflow_step_id: str | None = None

    @model_validator(mode="after")
    def validate_target(self) -> "AgentCall":
        if self.call_type == "agent" and not self.agent_id.strip():
            raise ValueError("agent call requires agent_id")
        if self.call_type == "tool" and not self.tool_id.strip():
            raise ValueError("tool call requires tool_id")
        return self

    @property
    def target_id(self) -> str:
        return self.tool_id if self.call_type == "tool" else self.agent_id


class SupervisorPlan(BaseModel):
    user_goal: str
    required_evidence: list[str] = Field(default_factory=list)
    calls: list[AgentCall] = Field(default_factory=list)
    planning_note: str = ""


class SupervisorVerdict(BaseModel):
    verdict: SupervisorVerdictType
    answerable: bool = False
    confirmed_facts: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    final_answer_draft: str = ""
    next_call: AgentCall | None = None
    next_calls: list[AgentCall] = Field(default_factory=list)
    reason_summary: str = ""
