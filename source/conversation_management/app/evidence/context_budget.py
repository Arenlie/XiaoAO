from __future__ import annotations

import json
from typing import Any


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def _bound(value: Any, *, budget: int, depth: int = 0) -> Any:
    """Budget a single context component and make every truncation explicit."""
    if budget <= 64:
        return {"truncated": True, "reason": "context_budget_exhausted"}
    if _size(value) <= budget:
        return value
    if depth > 8:
        return {"truncated": True, "reason": "nested_context_budget"}
    if isinstance(value, str):
        keep = max(0, budget - 120)
        return {
            "text": value[:keep],
            "original_chars": len(value),
            "returned_chars": min(len(value), keep),
            "truncated": len(value) > keep,
        }
    if isinstance(value, list):
        if not value:
            return []
        overhead = 120
        per = max(128, (budget - overhead) // min(len(value), 24))
        out = []
        for item in value:
            candidate = _bound(item, budget=per, depth=depth + 1)
            out.append(candidate)
            envelope = {
                "items": out,
                "total_count": len(value),
                "returned_count": len(out),
                "truncated": len(out) < len(value),
                "has_more": len(out) < len(value),
            }
            if _size(envelope) >= budget or len(out) >= 24:
                break
        return {
            "items": out,
            "total_count": len(value),
            "returned_count": len(out),
            "truncated": len(out) < len(value),
            "has_more": len(out) < len(value),
        }
    if isinstance(value, dict):
        if not value:
            return {}
        keys = list(value.keys())
        per = max(128, (budget - 160) // min(len(keys), 24))
        out: dict[str, Any] = {}
        for key in keys:
            out[str(key)] = _bound(value[key], budget=per, depth=depth + 1)
            if _size(out) >= budget or len(out) >= 24:
                break
        if len(out) < len(keys):
            out["_context_budget"] = {
                "truncated": True,
                "returned_fields": len(out),
                "total_fields": len(keys),
                "omitted_fields": [str(x) for x in keys[len(out): len(out) + 12]],
            }
        return out
    return value


class ContextBudgetManager:
    """Build a bounded planner/answer context without opaque whole-JSON slicing."""

    DEFAULT_BUDGETS = {
        "runtime_clock": 1200,
        "query": 3000,
        "recent_messages": 7000,
        "memory_context": 5000,
        "user_profile": 1500,
        "active_entity": 1800,
        "entity_resolution": 2500,
        "selected_entity": 1800,
        "resolved_entity": 1800,
        "entity_result": 3000,
        "query_scope": 1800,
        "entity_constraints": 1800,
        "business_intent": 6000,
        "business_workflow": 2200,
        "active_diagnosis_task": 1200,
        "content_understanding": 6500,
        "topic_workspace": 3000,
        "evidence_catalog": 13000,
        "task_delta": 2200,
        "evidence_requirements": 5000,
        "evidence_ledger": 3000,
        "completion_evaluation": 5000,
        "observations": 9000,
    }

    def __init__(self, *, max_chars: int = 76000) -> None:
        self.max_chars = max_chars

    def compile(self, data: dict[str, Any]) -> str:
        bounded: dict[str, Any] = {}
        for key, value in data.items():
            budget = self.DEFAULT_BUDGETS.get(key, 2500)
            bounded[key] = _bound(value, budget=budget)
        # Individual component budgets make overflow rare. If model configuration is
        # exceptionally small, reduce low-priority components explicitly and report it.
        while _size(bounded) > self.max_chars:
            changed = False
            for key in ("recent_messages", "observations", "content_understanding", "memory_context", "user_profile"):
                if key not in bounded:
                    continue
                previous = bounded[key]
                reduced = _bound(previous, budget=max(500, self.DEFAULT_BUDGETS.get(key, 2500) // 2))
                if _size(reduced) < _size(previous):
                    bounded[key] = reduced
                    changed = True
                    if _size(bounded) <= self.max_chars:
                        break
            if not changed:
                # Never silently cut bytes. Remove only a documented low-priority
                # section and leave a marker visible to the model.
                for key in ("user_profile", "memory_context", "recent_messages"):
                    if key in bounded and not (isinstance(bounded[key], dict) and bounded[key].get("omitted")):
                        bounded[key] = {"omitted": True, "reason": "global_context_budget"}
                        changed = True
                        break
            if not changed:
                break
        return json.dumps(bounded, ensure_ascii=False, default=str)
