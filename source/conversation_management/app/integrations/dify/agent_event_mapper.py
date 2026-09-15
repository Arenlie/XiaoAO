from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class UnifiedAgentEvent:
    event: str
    data: dict[str, Any] = field(default_factory=dict)
    persist: bool = False


def _first_nonempty(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def map_dify_event(payload: dict[str, Any]) -> list[UnifiedAgentEvent]:
    event_type = str(payload.get("event") or "")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}

    if event_type == "ping":
        return [UnifiedAgentEvent("heartbeat", {})]

    if event_type == "agent_thought":
        events: list[UnifiedAgentEvent] = []
        thought = _first_nonempty(
            payload.get("thought"),
            payload.get("answer"),
            payload.get("reasoning"),
            data.get("thought"),
            data.get("answer"),
            data.get("reasoning"),
        )
        if thought:
            events.append(
                UnifiedAgentEvent(
                    "reasoning.delta",
                    {
                        "content": str(thought),
                        "iteration": payload.get("iteration") or data.get("iteration"),
                        "message_id": payload.get("message_id"),
                        "position": payload.get("position"),
                        "channel": "reasoning",
                    },
                    persist=True,
                )
            )

        tool = _first_nonempty(payload.get("tool"), data.get("tool"))
        tool_input = _first_nonempty(payload.get("tool_input"), data.get("tool_input"))
        observation = _first_nonempty(payload.get("observation"), data.get("observation"))
        if tool and observation is None:
            events.append(
                UnifiedAgentEvent(
                    "tool.started",
                    {
                        "tool_name": str(tool),
                        "input": tool_input,
                        "iteration": payload.get("iteration") or data.get("iteration"),
                    },
                    persist=True,
                )
            )
        if tool and observation is not None:
            events.append(
                UnifiedAgentEvent(
                    "tool.completed",
                    {
                        "tool_name": str(tool),
                        "input": tool_input,
                        "output": observation,
                        "iteration": payload.get("iteration") or data.get("iteration"),
                    },
                    persist=True,
                )
            )
        return events

    if event_type in {"agent_message", "message"}:
        answer = payload.get("answer")
        return (
            [UnifiedAgentEvent("answer.delta", {"content": str(answer or ""), "channel": "final"})]
            if answer
            else []
        )

    if event_type == "message_replace":
        answer = payload.get("answer") or ""
        return [UnifiedAgentEvent("answer.replace", {"content": str(answer), "channel": "final"})]

    if event_type == "message_end":
        return [
            UnifiedAgentEvent(
                "reasoning.completed",
                {
                    "conversation_id": payload.get("conversation_id"),
                    "message_id": payload.get("message_id"),
                },
            ),
            UnifiedAgentEvent(
                "answer.completed",
                {
                    "metadata": payload.get("metadata") or {},
                    "conversation_id": payload.get("conversation_id"),
                    "message_id": payload.get("message_id"),
                    "channel": "final",
                },
            ),
        ]

    if event_type in {
        "workflow_started",
        "node_started",
        "node_finished",
        "workflow_finished",
    }:
        return [
            UnifiedAgentEvent(
                "analysis.step",
                {"type": event_type, "data": payload.get("data") or {}},
                persist=True,
            )
        ]

    if event_type == "error":
        return [
            UnifiedAgentEvent(
                "task.failed",
                {
                    "code": payload.get("code") or "AGENT_STREAM_ERROR",
                    "message": payload.get("message") or "Dify Agent 返回错误",
                    "status": payload.get("status"),
                },
            )
        ]
    return []
