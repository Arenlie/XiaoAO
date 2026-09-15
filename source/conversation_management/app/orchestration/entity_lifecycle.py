from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Literal, Mapping

from app.tools.alarm_context import (
    active_entity_matches_query,
    derive_context_area_target,
    entity_satisfies_required_level,
)

EntityAction = Literal["reuse", "replace", "none"]

# Conservative detector for explicit PHM/entity codes such as BB1LG1803005 or
# QY004002001001.  It intentionally requires both a letter and a digit so normal
# Chinese/English prose is not interpreted as an entity switch.
_EXPLICIT_CODE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?=[A-Za-z0-9_-]{6,}(?![A-Za-z0-9]))"
    r"(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*\d)"
    r"[A-Za-z][A-Za-z0-9_-]{5,}(?![A-Za-z0-9])",
    re.IGNORECASE,
)

# These fields describe the spatial scope of a known entity and are safe to retain
# as a resolver hint when the user explicitly switches from equipment A to equipment B.
# Device/point identifiers are deliberately removed so old equipment cannot constrain
# the fresh fuzzy lookup.
_SCOPE_KEYS = {
    "group_name",
    "company_name",
    "plant_name",
    "region_name",
    "area_name",
    "workshop_name",
    "line_name",
    "leaf_space_name",
    "leaf_space_type",
    "space_id",
    "space_name",
    "space_path",
    "space_link",
    "space_number",
}
_DEVICE_ID_KEYS = {
    "device_code",
    "device_id",
    "equip_no",
    "equip_id",
    "equipment_no",
    "equipNo",
    "equip_name",
    "equipment_name",
    "point_no",
    "point_id",
    "pointNo",
    "pointId",
    "wave_point_no",
    "wave_point_code",
    "feature_point_id",
    "temperature_point_no",
    "temperature_point_id",
}


def _merged(entity: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(entity or {})
    metadata = source.get("metadata")
    if isinstance(metadata, Mapping):
        source = {**dict(metadata), **source}
    return source


def _normalize(value: Any) -> str:
    return re.sub(r"[\s/#号\\>｜|\-—_（）()·.]", "", str(value or "").lower())


def _active_codes(entity: Mapping[str, Any] | None) -> set[str]:
    source = _merged(entity)
    result: set[str] = set()
    for key in ("device_code", "equip_no", "equipment_no", "equipNo", "point_no", "pointNo"):
        value = str(source.get(key) or "").strip()
        if value:
            result.add(value.upper())
    return result


def has_explicit_different_code(query: str, active_entity: Mapping[str, Any] | None) -> bool:
    """Return True when the current utterance explicitly names a code other than the active one."""

    from app.services.sensor_references import code_tokens
    codes = {item.upper() for item in code_tokens(query)}
    if not codes:
        return False
    active_codes = _active_codes(active_entity)
    if not active_codes:
        # A newly supplied explicit code is stronger evidence than an old area-only
        # context and should trigger entity resolution for the named object.
        return True
    return not codes.issubset(active_codes)


def build_scope_hint(entity: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build a spatial-only resolver hint from a previous equipment/point entity."""

    original = dict(entity or {})
    merged = _merged(entity)
    hint = {key: merged[key] for key in _SCOPE_KEYS if merged.get(key) not in (None, "")}
    # Preserve metadata only with scope fields, never old device/point IDs.
    metadata = original.get("metadata")
    if isinstance(metadata, Mapping):
        scope_metadata = {
            key: value
            for key, value in metadata.items()
            if key in _SCOPE_KEYS and value not in (None, "")
        }
        if scope_metadata:
            hint["metadata"] = scope_metadata
    for key in _DEVICE_ID_KEYS:
        hint.pop(key, None)
    if hint:
        hint.setdefault("entity_type", "space")
    return hint


@dataclass(slots=True)
class EntityResolutionDecision:
    action: EntityAction
    source: str
    anchor_entity: dict[str, Any]
    scope_hint: dict[str, Any]
    reason: str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def decide_entity_resolution(
    *,
    query: str,
    active_entity: Mapping[str, Any] | None,
    selected_entity: Mapping[str, Any] | None = None,
) -> EntityResolutionDecision:
    """Decide whether this turn reuses the branch entity or resolves a new one.

    This is deliberately *not* a task/intent classifier.  It only manages entity
    continuity, so a later planner may misclassify a request without losing the device
    identity.  Explicit entity changes still remain possible.
    """

    selected = dict(selected_entity or {})
    if selected and not has_explicit_different_code(query, selected):
        return EntityResolutionDecision(
            action="reuse",
            source="user_selection",
            anchor_entity=selected,
            scope_hint=build_scope_hint(selected),
            reason="用户本轮已明确选择实体，优先使用该实体。",
            confidence=1.0,
        )

    active = dict(active_entity or {})
    if not active:
        return EntityResolutionDecision(
            action="none",
            source="none",
            anchor_entity={},
            scope_hint={},
            reason="当前分支没有可继承的实体。",
            confidence=1.0,
        )

    text = str(query or "").strip()
    if not text:
        return EntityResolutionDecision(
            action="reuse",
            source="context",
            anchor_entity=active,
            scope_hint=build_scope_hint(active),
            reason="本轮没有新的实体表达，沿用当前实体。",
            confidence=0.95,
        )

    if has_explicit_different_code(text, active):
        return EntityResolutionDecision(
            action="replace",
            source="user_input",
            anchor_entity={},
            scope_hint={},
            reason="用户本轮显式提供了与当前实体不同的编码，需要重新解析实体。",
            confidence=1.0,
        )

    # Scope transition is different from entity replacement.  A follow-up may refer
    # to an ancestor already present in the equipment hierarchy (for example
    # “第一炼钢事业部有哪些报警”). Promote the anchor to that real space scope so
    # downstream alarm/data tools cannot keep the old equip_no filter.
    area_target = derive_context_area_target(text, active)
    if area_target:
        return EntityResolutionDecision(
            action="reuse",
            source="context_upscope",
            anchor_entity=area_target,
            scope_hint=build_scope_hint(area_target),
            reason="用户将查询范围从当前设备明确上提到其已知上级区域，复用层级事实并切换为区域实体。",
            confidence=1.0,
        )

    if active_entity_matches_query(text, active):
        return EntityResolutionDecision(
            action="reuse",
            source="context",
            anchor_entity=active,
            scope_hint=build_scope_hint(active),
            reason="当前问题与分支实体一致或使用指代表达，复用实体。",
            confidence=0.95,
        )

    return EntityResolutionDecision(
        action="replace",
        source="user_input",
        anchor_entity={},
        scope_hint={},
        reason="当前问题包含与分支实体不一致的新实体表达，允许重新解析。",
        confidence=0.95,
    )



def _fresh_fuzzy_resolved_entity(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return an entity produced by fuzzy resolution in the *current turn*.

    ``resolved_entity`` is persisted across turns and therefore cannot, by itself, prove
    that the current query has been resolved.  On REPLACE/NONE turns only a current-turn
    fuzzy observation (or an explicit user selection handled separately) may satisfy a
    downstream PHM tool's identity requirement.
    """

    observations = state.get("observations")
    if not isinstance(observations, list):
        return {}
    for item in reversed(observations):
        if not isinstance(item, Mapping):
            continue
        if str(item.get("agent_id") or "") != "builtin.fuzzy_entity":
            continue
        if str(item.get("status") or "").upper() not in {"SUCCESS", "COMPLETED"}:
            continue
        updates = item.get("state_updates")
        if isinstance(updates, Mapping):
            entity = updates.get("resolved_entity")
            if isinstance(entity, Mapping) and entity:
                return dict(entity)
        # Normal executor observations may expose the resolver result through evidence
        # instead of state_updates.  Keep this fallback deterministic and local to the
        # current turn.
        evidence = item.get("evidence")
        if isinstance(evidence, list):
            for ev in reversed(evidence):
                if not isinstance(ev, Mapping):
                    continue
                result = ev.get("result")
                if isinstance(result, Mapping):
                    entity = result.get("resolved_entity")
                    if isinstance(entity, Mapping) and entity:
                        return dict(entity)
    return {}


def _entity_identity_signature(entity: Mapping[str, Any] | None) -> tuple[str, str] | None:
    source = _merged(entity)
    for key in (
        "point_no", "pointNo", "point_id", "pointId", "wave_point_no",
        "wave_point_code", "feature_point_id", "temperature_point_no",
        "temperature_point_id",
    ):
        value = str(source.get(key) or "").strip()
        if value:
            equipment = str(source.get("equip_no") or source.get("device_code") or "").upper()
            return ("point", equipment + "\0" + value.upper())
    for key in ("device_code", "equip_no", "equipment_no", "equipNo", "equip_id", "device_id"):
        value = str(source.get(key) or "").strip()
        if value:
            return ("equipment", value.upper())
    for key in ("space_id", "space_number", "space_link", "space_path", "space_name"):
        value = str(source.get(key) or "").strip()
        if value:
            return ("space", _normalize(value))
    return None


def _resolved_differs_from_old_anchor(state: Mapping[str, Any]) -> dict[str, Any]:
    """Accept a resolved entity on REPLACE only when it is demonstrably new.

    This supports expert/synthetic states where the resolver result has already been
    merged into ``resolved_entity`` but its observation is not retained.  A stale
    previous-turn resolved entity has the same identity signature as ``active_entity``
    and is therefore rejected.
    """

    resolved = state.get("resolved_entity")
    if not isinstance(resolved, Mapping) or not resolved:
        return {}
    active = state.get("active_entity")
    if not isinstance(active, Mapping) or not active:
        return dict(resolved)
    resolved_sig = _entity_identity_signature(resolved)
    active_sig = _entity_identity_signature(active)
    if resolved_sig and active_sig and resolved_sig != active_sig:
        return dict(resolved)
    return {}


def _current_turn_entity_unchecked(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return the entity that is authoritative for this user turn.

    The crucial rule is that a stale ``resolved_entity`` from a previous turn must not
    satisfy a new business call after ``entity_resolution.action == 'replace'``.  The
    current turn becomes resolved only after fuzzy succeeds (or the user explicitly
    selects a candidate).
    """

    selected = state.get("selected_entity")
    if isinstance(selected, Mapping) and selected:
        return dict(selected)

    decision = state.get("entity_resolution")
    decision = dict(decision) if isinstance(decision, Mapping) else {}
    action = str(decision.get("action") or "").lower()

    if action == "reuse":
        anchor = decision.get("anchor_entity")
        if isinstance(anchor, Mapping) and anchor:
            return dict(anchor)
        resolved = state.get("resolved_entity")
        if isinstance(resolved, Mapping) and resolved:
            return dict(resolved)
        return {}

    if action == "replace":
        fresh = _fresh_fuzzy_resolved_entity(state)
        if fresh:
            return fresh
        return _resolved_differs_from_old_anchor(state)

    if action == "none":
        fresh = _fresh_fuzzy_resolved_entity(state)
        if fresh:
            return fresh
        resolved = state.get("resolved_entity")
        return dict(resolved) if isinstance(resolved, Mapping) and resolved else {}

    # Backward-compatible fallback for old/synthetic states that predate R26.
    resolved = state.get("resolved_entity")
    if isinstance(resolved, Mapping) and resolved:
        return dict(resolved)
    active = state.get("active_entity")
    if (
        isinstance(active, Mapping)
        and active
        and active_entity_matches_query(str(state.get("query") or ""), active)
    ):
        return dict(active)
    return {}


def current_turn_entity(state: Mapping[str, Any]) -> dict[str, Any]:
    from app.services.sensor_references import code_tokens, matches_tokens
    target = state.get("sensor_target") or {}
    verified_asset = state.get("resolved_entity") or {}
    if (target.get("verified") and target.get("asset_identity_available")
            and matches_tokens(target, code_tokens(state.get("query")))
            and (state.get("entity_result") or {}).get("resolution_source") == "sensor_identity_verification"
            and str(verified_asset.get("equip_no") or "").casefold() == str(target.get("equip_num") or "").casefold()):
        return dict(verified_asset)
    entity = _current_turn_entity_unchecked(state)
    if has_explicit_different_code(str(state.get("query") or ""), entity):
        target = state.get("sensor_target") or {}
        if (target.get("verified") and matches_tokens(target, code_tokens(state.get("query")))
                and str(entity.get("equip_no") or "").casefold() == str(target.get("equip_num") or "").casefold()):
            return entity
        return {}
    return entity


def has_current_turn_entity_for_level(
    state: Mapping[str, Any], required_level: str
) -> bool:
    """Whether the *current turn* has a valid entity at the requested level."""

    return entity_satisfies_required_level(current_turn_entity(state), required_level)

def entity_anchor(state: Mapping[str, Any]) -> dict[str, Any]:
    decision = state.get("entity_resolution")
    if isinstance(decision, Mapping) and str(decision.get("action") or "") == "reuse":
        anchor = decision.get("anchor_entity")
        if isinstance(anchor, Mapping) and anchor:
            return dict(anchor)
    for key in ("selected_entity", "resolved_entity"):
        value = state.get(key)
        if isinstance(value, Mapping) and value:
            return dict(value)
    return {}


def can_down_drill_equipment_to_point(state: Mapping[str, Any]) -> bool:
    """Whether a point-scoped call can be resolved inside this turn's equipment.

    The previous implementation allowed down-drill only when lifecycle.action was
    ``reuse``.  A fresh Asset-MCP resolution is recorded as ``replace``, so a later
    planner fuzzy call could accidentally launch a second global entity lookup.
    ``current_turn_entity`` already rejects stale previous-turn anchors; use it as the
    authoritative guard instead.
    """

    anchor = current_turn_entity(state)
    return entity_satisfies_required_level(anchor, "equipment") and not entity_satisfies_required_level(anchor, "point")
