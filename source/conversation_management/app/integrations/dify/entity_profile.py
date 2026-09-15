from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

AREA_KEYS = (
    "responsible_areas",
    "preferred_areas",
    "focus_areas",
    "areas",
    "area_names",
    "frequent_areas",
    "responsible_area",
)
EQUIPMENT_KEYS = (
    "responsible_equipment",
    "responsible_equipments",
    "frequent_equipment",
    "preferred_equipment",
    "equipments",
    "equipment_list",
)
NESTED_KEYS = ("profile_json", "asset_profile", "business_context")


def _clean_text(value: Any, *, max_length: int = 256) -> str:
    if value is None:
        return ""
    return str(value).strip()[:max_length]


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _dedupe(values: list[Any], limit: int) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        marker = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if marker in seen:
            continue
        seen.add(marker)
        result.append(value)
        if len(result) >= limit:
            break
    return result


def _normalize_area_item(value: Any) -> str | dict[str, str] | None:
    if isinstance(value, str):
        text = _clean_text(value)
        return text or None
    if not isinstance(value, Mapping):
        return None
    allowed = (
        "area_name",
        "area",
        "region_name",
        "region",
        "factory_name",
        "workshop_name",
        "line_name",
        "production_line",
        "name",
    )
    item = {key: _clean_text(value.get(key)) for key in allowed if _clean_text(value.get(key))}
    return item or None


def _normalize_equipment_item(value: Any) -> str | dict[str, str] | None:
    if isinstance(value, str):
        text = _clean_text(value)
        return text or None
    if not isinstance(value, Mapping):
        return None
    allowed = (
        "equip_no",
        "equipment_no",
        "code",
        "equip_name",
        "equipment_name",
        "name",
        "area_name",
        "area",
        "region_name",
        "factory_name",
        "workshop_name",
        "line_name",
    )
    item = {key: _clean_text(value.get(key)) for key in allowed if _clean_text(value.get(key))}
    return item or None


def build_entity_workflow_profile(
    profile: Mapping[str, Any] | None,
    *,
    max_items_per_field: int = 20,
    max_json_characters: int = 12000,
) -> dict[str, Any]:
    """Build the allow-listed profile passed to the profile-aware entity workflow.

    The Agent may receive the complete user profile, while entity lookup receives only
    area/equipment weak-prior fields. This avoids leaking unrelated profile fields and
    keeps the Dify input below its 12,000-character limit.
    """

    source = dict(profile or {})
    result: dict[str, Any] = {}

    for key in AREA_KEYS:
        normalized = [
            item
            for raw in _as_list(source.get(key))
            if (item := _normalize_area_item(raw)) is not None
        ]
        if normalized:
            result[key] = _dedupe(normalized, max_items_per_field)

    for key in EQUIPMENT_KEYS:
        normalized = [
            item
            for raw in _as_list(source.get(key))
            if (item := _normalize_equipment_item(raw)) is not None
        ]
        if normalized:
            result[key] = _dedupe(normalized, max_items_per_field)

    for key in NESTED_KEYS:
        nested = source.get(key)
        if isinstance(nested, Mapping):
            nested_profile = build_entity_workflow_profile(
                nested,
                max_items_per_field=max_items_per_field,
                max_json_characters=max_json_characters,
            )
            if nested_profile:
                result[key] = nested_profile

    def serialized_length() -> int:
        return len(
            json.dumps(
                result,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    def list_paths(value: Any, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
        paths: list[tuple[str, ...]] = []
        if isinstance(value, dict):
            for key, child in value.items():
                if isinstance(child, list) and child:
                    paths.append((*prefix, key))
                elif isinstance(child, dict):
                    paths.extend(list_paths(child, (*prefix, key)))
        return paths

    def value_at(path: tuple[str, ...]) -> list[Any]:
        current: Any = result
        for key in path:
            current = current[key]
        return current

    # Deterministic global trimming, including nested profile_json/asset_profile fields.
    while serialized_length() > max_json_characters:
        paths = list_paths(result)
        if not paths:
            return {}
        path = max(paths, key=lambda item: (len(value_at(item)), item))
        values = value_at(path)
        values.pop()

        current: dict[str, Any] = result
        parents: list[tuple[dict[str, Any], str]] = []
        for key in path[:-1]:
            parents.append((current, key))
            current = current[key]
        if not values:
            current.pop(path[-1], None)
        for parent, key in reversed(parents):
            child = parent.get(key)
            if isinstance(child, dict) and not child:
                parent.pop(key, None)

    return result


def serialize_entity_workflow_profile(
    profile: Mapping[str, Any] | None, *, max_json_characters: int = 12000
) -> str:
    return json.dumps(
        build_entity_workflow_profile(
            profile, max_json_characters=max_json_characters
        ),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
