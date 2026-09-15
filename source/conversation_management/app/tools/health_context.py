from __future__ import annotations

import re
from typing import Any, Mapping

HEALTH_QUERY_INTENT = "HEALTH_QUERY"
DEVICE_HEALTH_INTENT = "DEVICE_HEALTH"
SPACE_HEALTH_INTENT = "SPACE_HEALTH"

_HEALTH_PATTERNS = (
    "健康度",
    "健康评分",
    "健康分",
    "健康等级",
    "健康情况",
    "健康状态",
    "健康吗",
    "健康不健康",
)

def is_health_query(query: str) -> bool:
    text = re.sub(r"\s+", "", str(query or ""))
    return any(token in text for token in _HEALTH_PATTERNS)




def health_query_requests_diagnosis(query: str) -> bool:
    """Whether the user explicitly asks for a causal/professional health diagnosis.

    Merely asking to "分析一下这个健康度数据" is *not* a diagnosis request. That is
    contextual interpretation and should be handled by the general model using the
    already returned health result. A diagnosis is reserved for causal questions such
    as "为什么健康度下降" or "健康分降低是什么原因".
    """

    if not is_health_query(query):
        return False
    text = re.sub(r"\s+", "", str(query or ""))
    return any(
        token in text
        for token in (
            "为什么",
            "什么原因",
            "下降原因",
            "降低原因",
            "变差原因",
            "扣分原因",
            "异常原因",
            "怎么回事",
            "为何",
        )
    )

def health_query_requests_history(
    query: str,
    call_arguments: Mapping[str, Any] | None = None,
) -> bool:
    """Return whether the model supplied an explicit historical time range.

    Natural-language temporal semantics are owned by the LLM.  This backend helper
    intentionally does not inspect query text for "最近N天/昨天/本月" or similar
    expressions; it only observes already-structured absolute arguments.
    """

    args = dict(call_arguments or {})
    return bool(
        args.get("start_time") not in (None, "")
        or args.get("end_time") not in (None, "")
    )



def space_health_history_unsupported(
    scope_type: str,
    query: str,
    call_arguments: Mapping[str, Any] | None = None,
) -> bool:
    return str(scope_type or "").strip().lower() == "space" and health_query_requests_history(
        query, call_arguments
    )

def _merged_entity(entity: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(entity or {})
    metadata = source.get("metadata")
    if isinstance(metadata, Mapping):
        source = {**dict(metadata), **source}
    return source


def _semantic_goal_frame(business_intent: Mapping[str, Any] | None) -> dict[str, Any]:
    intent = dict(business_intent or {})
    goal = intent.get("goal_frame")
    return dict(goal) if isinstance(goal, Mapping) else {}


def _semantic_asset_hints(business_intent: Mapping[str, Any] | None) -> dict[str, Any]:
    intent = dict(business_intent or {})
    hints = intent.get("asset_semantics")
    return dict(hints) if isinstance(hints, Mapping) else {}


def health_collection_target_level(
    business_intent: Mapping[str, Any] | None,
) -> str:
    """Return a structured descendant health target, or an empty string.

    This deliberately consumes only Task Understanding output.  It never classifies
    Chinese words such as “粗轧区/车间/设备” with regexes.  Therefore these two goals
    remain distinct even though both mention the same root space:

    - root space health: target=space, no descendant collection;
    - health of devices under a root space: descendant_target_level=equipment.
    """

    goal = _semantic_goal_frame(business_intent)
    hints = _semantic_asset_hints(business_intent)
    evidence = {str(x).strip().lower() for x in goal.get("evidence_types") or []}
    requested = {str(x).strip().lower() for x in goal.get("requested_evidence_types") or []}
    if "health" not in evidence and "health" not in requested:
        return ""
    if not bool(hints.get("descendant_collection_requested")):
        return ""
    level = str(hints.get("descendant_target_level") or "").strip().lower()
    if level == "area":
        level = "space"
    return level if level in {"space", "equipment"} else ""


def infer_health_scope_type(
    query: str,
    call_arguments: Mapping[str, Any] | None = None,
    entity: Mapping[str, Any] | None = None,
    business_intent: Mapping[str, Any] | None = None,
) -> str:
    """Return Data-MCP health scope type from structured semantics and real identity.

    No natural-language area/device regex is used here.  The semantic hierarchy comes
    from Task Understanding; a resolved Asset-MCP entity is the factual identity
    fallback.  ``query`` remains only for API compatibility.
    """

    del query
    args = dict(call_arguments or {})
    explicit = str(args.get("scope_type") or "").strip().lower()
    if explicit in {"device", "space"}:
        return explicit

    goal = _semantic_goal_frame(business_intent)
    target = str(goal.get("target_entity_level") or "").strip().lower()
    anchor = str(goal.get("anchor_entity_level") or "").strip().lower()
    if target == "area":
        target = "space"
    if anchor == "area":
        anchor = "space"

    # A descendant collection is executed by query_scope_collection + batch health;
    # this function is for a *single* health object.  For non-collection goals the
    # semantic target is authoritative.
    if not health_collection_target_level(business_intent):
        if target == "space":
            return "space"
        if target in {"equipment", "point"}:
            return "device"
        if anchor == "space":
            return "space"
        if anchor in {"equipment", "point"}:
            return "device"

    source = _merged_entity(entity)
    entity_type = str(
        source.get("entity_type")
        or source.get("match_type")
        or source.get("node_type")
        or ""
    ).strip().lower()
    if entity_type in {"area", "region", "plant", "line", "space", "workshop", "company"}:
        return "space"
    if entity_type in {"equipment", "device", "point"}:
        return "device"

    # Canonical database identities are a safe factual fallback; names are not parsed.
    if any(source.get(k) not in (None, "") for k in ("equip_no", "device_code", "equipment_no", "equip_id", "device_id")):
        return "device"
    if any(source.get(k) not in (None, "") for k in ("space_id", "spaceId")):
        return "space"
    return "device"


def health_required_entity_level(
    query: str,
    scope_type: str | None,
    requested_level: str | None = None,
) -> str:
    """Map semantic/Data scope to the legacy executor identity contract.

    The runtime still calls a space root requirement ``area`` for compatibility with
    Asset MCP.  This mapping is structural and contains no natural-language rules.
    """

    del query
    scope = str(scope_type or "").strip().lower()
    if scope == "space":
        return "area"
    if scope == "device":
        return "equipment"

    requested = str(requested_level or "none").strip().lower()
    if requested in {"space", "area"}:
        return "area"
    if requested in {"equipment", "point"}:
        return "equipment"
    return "equipment"


def health_intent_kind(query: str, scope_type: str) -> str:
    if not is_health_query(query):
        return ""
    return SPACE_HEALTH_INTENT if scope_type == "space" else DEVICE_HEALTH_INTENT


def infer_health_time_range(
    query: str,
    call_arguments: Mapping[str, Any] | None = None,
    **_: Any,
) -> tuple[str | None, str | None]:
    """Return only explicit absolute range values already produced by the LLM.

    Kept as a compatibility helper for existing callers.  It performs no
    natural-language parsing and no date arithmetic.
    """

    args = dict(call_arguments or {})
    start_arg = str(args.get("start_time") or "").strip() or None
    end_arg = str(args.get("end_time") or "").strip() or None
    return start_arg, end_arg
