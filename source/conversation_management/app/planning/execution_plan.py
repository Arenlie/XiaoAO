from __future__ import annotations

from typing import Any


def build_execution_plan(*, requirements: list[dict[str, Any]], completion: dict[str, Any], capability_hints: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "requirements": requirements,
        "completion_status": completion.get("status"),
        "gaps": list(completion.get("gaps") or []),
        "capability_hints": capability_hints,
    }
