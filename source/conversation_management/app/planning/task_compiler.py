from __future__ import annotations

from typing import Any

from app.evidence.models import EvidenceRequirement
from app.planning.requirements import LEGACY_TO_SEMANTIC, requirement_for


_STABLE_SUBJECT_KEYS = {
    "entity_type", "equip_no", "equipment_no", "point_no", "pointNo",
    "space_id", "attachment_id", "file_id",
}
_STABLE_SCOPE_KEYS = {
    "root_space_id", "space_id", "equip_no", "point_no", "start_time", "end_time",
    "time_start", "time_end", "fault_time",
}


def _constraint(value: dict[str, Any] | None, allowed: set[str]) -> dict[str, Any]:
    return {
        str(k): v for k, v in (value or {}).items()
        if str(k) in allowed and v not in (None, "", [], {})
    }


class TaskCompiler:
    def compile(
        self,
        intent: dict[str, Any],
        *,
        subject_constraint: dict[str, Any] | None = None,
        scope_constraint: dict[str, Any] | None = None,
    ) -> list[EvidenceRequirement]:
        goal = intent.get("goal_frame") if isinstance(intent.get("goal_frame"), dict) else {}
        contract = intent.get("completion_contract") if isinstance(intent.get("completion_contract"), dict) else {}
        all_required_fields = [str(x) for x in contract.get("required_fields") or [] if str(x)]
        allow_partial = bool(contract.get("allow_partial", True))
        raw_types = [str(x).strip().lower() for x in goal.get("evidence_types") or [] if str(x).strip()]
        plan = intent.get("query_plan") if isinstance(intent.get("query_plan"), dict) else {}
        output_family = str(plan.get("domain") or "").lower()
        refresh_requested = bool(((intent.get("asset_semantics") or {}).get("refresh_requested")) if isinstance(intent.get("asset_semantics"), dict) else False)
        freshness_mode = "force_refresh" if refresh_requested else "reuse_if_valid"
        subject = _constraint(subject_constraint, _STABLE_SUBJECT_KEYS)
        scope = _constraint(scope_constraint, _STABLE_SCOPE_KEYS)
        out: list[EvidenceRequirement] = []
        seen: set[tuple[str, ...]] = set()
        for raw in raw_types:
            candidates = list(dict.fromkeys(LEGACY_TO_SEMANTIC.get(raw, [raw])))
            if not candidates:
                continue
            family_key = tuple(candidates)
            if family_key in seen:
                continue
            seen.add(family_key)
            semantic = candidates[0]
            fields = all_required_fields if len(raw_types) == 1 or raw == output_family else []
            out.append(requirement_for(
                semantic,
                family=raw,
                acceptable_semantic_types=candidates,
                subject_constraint=subject,
                scope_constraint=scope,
                required_fields=fields,
                allow_partial=allow_partial,
                freshness_mode=freshness_mode,
            ))
        return out
