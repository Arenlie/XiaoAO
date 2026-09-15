from __future__ import annotations

from typing import Any

from app.evidence.models import TaskDelta


def build_task_delta(intent: dict[str, Any]) -> TaskDelta:
    context = intent.get("context_resolution") if isinstance(intent.get("context_resolution"), dict) else {}
    goal = intent.get("goal_frame") if isinstance(intent.get("goal_frame"), dict) else {}
    contract = intent.get("completion_contract") if isinstance(intent.get("completion_contract"), dict) else {}
    operations = [str(x).strip().lower() for x in goal.get("operations") or [] if str(x).strip()]
    goal_action = operations[0] if operations else str(goal.get("output_type") or "answer")
    return TaskDelta(
        context_action=str(context.get("action") or "NEW_TOPIC"),
        subject_resolution=("reuse_current" if str(context.get("action") or "").startswith(("CONTINUE", "RESUME")) else "resolve_or_reuse"),
        goal_action=goal_action,
        target_evidence_types=[str(x) for x in goal.get("evidence_types") or []],
        freshness_requirement=("refresh" if (intent.get("asset_semantics") or {}).get("refresh_requested") else "reuse_if_valid"),
        preserve_subject=bool(str(context.get("action") or "").startswith(("CONTINUE", "RESUME"))),
        preserve_scope=bool(contract.get("collection_action") == "preserve" or contract.get("preserve_members")),
        output_requirement={
            "output_type": contract.get("output_type") or goal.get("output_type"),
            "response_mode": contract.get("response_mode"),
            "required_fields": list(contract.get("required_fields") or []),
            "result_scope": contract.get("result_scope"),
        },
    )
