from __future__ import annotations

from typing import Any, Mapping

from app.integrations.dify.entity_scope import enrich_query_scope
from app.orchestration.entity_postrank import candidate_equipment_identity
from app.tools.alarm_context import entity_satisfies_required_level


def build_selected_entity_result(
    previous: Mapping[str, Any] | None,
    selected: Mapping[str, Any],
) -> dict[str, Any]:
    """Turn a server-validated candidate choice into the final identity for this turn.

    The original Asset MCP policy metadata is retained for auditability.  In
    particular, ``query_fingerprint`` must survive selection so a resumed task can
    prove that the choice belongs to the same user question.
    """

    prior = dict(previous or {})
    entity = dict(selected)
    prior_decision = (
        dict(prior.get("decision") or {})
        if isinstance(prior.get("decision"), Mapping)
        else {}
    )
    query_scope = enrich_query_scope(
        prior.get("query_scope")
        if isinstance(prior.get("query_scope"), Mapping)
        else {},
        entity,
    )
    previous_source = str(prior.get("resolution_source") or "")
    decision = {
        **prior_decision,
        "action": "REUSE",
        "need_lookup": False,
        "should_refresh": False,
        "context_used": True,
        "reason": "用户已从本任务的数据库候选中明确选择实体。",
    }
    result = {
        **prior,
        "success": True,
        "status": "UNIQUE",
        "legacy_status": "UNIQUE",
        "need_lookup": False,
        "needs_disambiguation": False,
        "need_disambiguation": False,
        "matches": [entity],
        "matches_json": "",
        "match_count": 1,
        "candidate_count": 1,
        "top_similarity": float(
            entity.get("similarity") or entity.get("score") or 0.0
        ),
        "resolved_entity": entity,
        "resolved_entities": [entity],
        "selected_from_multiple": True,
        "query_scope": query_scope,
        "decision": decision,
        "return_mode": "single",
        "resolution_source": "user_selection",
        "message": "已采用用户从本任务候选列表中确认的真实资产实体。",
    }
    if previous_source and previous_source != "user_selection":
        result["candidate_resolution_source"] = previous_source
    return result


def selection_required_entity_level(
    business_workflow: Mapping[str, Any] | None,
    entity_result: Mapping[str, Any] | None,
) -> str:
    workflow = dict(business_workflow or {})
    required = str(workflow.get("required_entity_level") or "").strip().lower()
    if required:
        return "area" if required in {"space", "line"} else required
    result = dict(entity_result or {})
    decision = result.get("decision")
    if isinstance(decision, Mapping):
        required = str(decision.get("target_entity_level") or "").strip().lower()
    return "area" if required in {"space", "line"} else required or "any"


def selected_entity_is_compatible(
    selected: Mapping[str, Any] | None,
    required_level: str,
) -> bool:
    entity = dict(selected or {})
    if not entity:
        return False
    level = str(required_level or "any").lower()
    if level == "any":
        return any(
            entity_satisfies_required_level(entity, candidate)
            for candidate in ("point", "equipment", "area")
        )
    return entity_satisfies_required_level(entity, level)


def points_for_selected_parent(
    previous: Mapping[str, Any], selected: Mapping[str, Any],
) -> list[dict[str, Any]] | None:
    """Narrow this task's already-recalled points by the server-validated parent.

    None means this is not a parent-selection stage. An empty list means the
    stored evidence is missing/invalid; it must not become an arbitrary point
    lookup or bypass the normal identity guard.
    """
    source = previous.get("candidate_resolution_source") or previous.get("resolution_source")
    if source != "parent_equipment_disambiguation":
        return None
    parent = candidate_equipment_identity(selected)
    if not parent or entity_satisfies_required_level(selected, "point"):
        return []
    points = previous.get("point_selection_candidates")
    if not isinstance(points, list):
        return []
    return [dict(item) for item in points if isinstance(item, Mapping)
            and entity_satisfies_required_level(item, "point")
            and candidate_equipment_identity(item) == parent]


def build_point_selection_result(
    previous: Mapping[str, Any], selected_parent: Mapping[str, Any],
    points: list[dict[str, Any]],
) -> dict[str, Any]:
    """Keep the original component/position constraints through staged selection."""
    prior = {key: value for key, value in previous.items() if key not in {
        "error", "point_selection_candidates", "candidate_resolution_source",
    }}
    scope = enrich_query_scope(prior.get("query_scope") or {}, dict(selected_parent))
    scope.update(target_entity_type="point", return_mode="candidates")
    decision = {**dict(prior.get("decision") or {}), "action": "DOWN_DRILL",
                "target_entity_level": "point", "lookup_scope": "point",
                "need_lookup": False, "context_used": True, "should_refresh": False,
                "reason": "按用户确认的设备筛选本任务已检索到的真实测点候选。"}
    scope["entity_resolution_decision"] = decision
    return {**prior, "status": "MULTIPLE", "legacy_status": "MULTIPLE", "success": True,
            "need_lookup": False, "need_disambiguation": True, "needs_disambiguation": True,
            "matches": points, "matches_json": "", "match_count": len(points), "candidate_count": len(points),
            "resolved_entity": None, "resolved_entities": [], "selected_from_multiple": False,
            "lookup_scope": "point", "return_mode": "candidates", "query_scope": scope,
            "decision": decision, "resolution_source": "point_candidate_disambiguation",
            "message": "已确认设备。请选择该设备下需要诊断的具体测点；确认后将继续读取数据并诊断。"}
