from __future__ import annotations

from typing import Any, Mapping

_AREA_KEYS = (
    "group_name",
    "company_name",
    "plant_name",
    "region_name",
    "area_name",
    "line_name",
    "space_path",
    "leaf_space_name",
    "leaf_space_type",
    "space_id",
    "space_link",
    "space_number",
)


def enrich_query_scope(
    query_scope: Mapping[str, Any] | None,
    resolved_entity: Mapping[str, Any] | None,
) -> dict[str, Any]:
    scope = dict(query_scope or {})
    entity = dict(resolved_entity or {})
    if not entity:
        return scope

    entity_type = str(
        entity.get("entity_type")
        or entity.get("match_type")
        or entity.get("leaf_space_type")
        or ""
    ).lower()
    scope_type = str(scope.get("scope_type") or "").upper()
    lookup_scope = str(scope.get("lookup_scope") or "").lower()
    is_area_scope = (
        scope_type == "AREA_DESCENDANTS"
        or lookup_scope == "area_aggregate"
        or entity_type in {"area", "region", "plant", "line", "space", "workshop"}
    )
    if is_area_scope:
        area = {key: entity.get(key) for key in _AREA_KEYS if entity.get(key) not in (None, "")}
        if area:
            scope["resolved_area"] = area
        scope.setdefault("scope_type", "AREA_DESCENDANTS")
        scope.setdefault("include_current_area", True)
        scope.setdefault("include_descendants", True)
        scope.setdefault("target_entity_type", "equipment")
    return scope
