from __future__ import annotations

from typing import Any, Mapping

from app.domain.enums import EntityStatus
from app.schemas.entity import EntityLookupResult

# Conversation must not infer asset identity from the user's free text.  Asset MCP is
# the single owner of natural-language entity understanding.  This module only
# projects already-returned real candidates to the entity level required by the
# workflow (for example point rows -> their authoritative parent equipment).

_EQUIPMENT_ID_KEYS = (
    "equip_no",
    "device_code",
    "equipment_no",
    "equipNo",
    "equip_id",
    "device_id",
)
_EQUIPMENT_COPY_KEYS = (
    "equip_no",
    "device_code",
    "equipment_no",
    "equipNo",
    "equip_id",
    "device_id",
    "equip_name",
    "equipment_name",
    "device_name",
    "equipment_type",
    "space_id",
    "space_name",
    "space_path",
    "space_link",
    "space_number",
    "workshop_name",
    "region_name",
    "plant_name",
    "company_name",
    "group_name",
    "line_name",
    "leaf_space_name",
    "leaf_space_type",
    "area_name",
)


def _candidate_merged(candidate: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    metadata = candidate.get("metadata")
    if isinstance(metadata, Mapping):
        merged.update(dict(metadata))
    data = candidate.get("data")
    if isinstance(data, Mapping):
        merged.update(dict(data))
    merged.update(dict(candidate))
    return merged


def _equipment_identity(candidate: Mapping[str, Any]) -> str | None:
    source = _candidate_merged(candidate)
    for key in _EQUIPMENT_ID_KEYS:
        value = source.get(key)
        if value not in (None, ""):
            return f"{key}:{value}"
    return None


def candidate_equipment_identity(candidate: Mapping[str, Any]) -> str | None:
    """Return the authoritative parent equipment identity of an entity candidate."""

    return _equipment_identity(candidate)


def _promote_candidate_to_equipment(
    candidate: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Collapse a point candidate to its real parent equipment.

    This is an identity projection only.  It never parses or compares the user's
    natural language and never invents an equipment code.
    """

    identity = _equipment_identity(candidate)
    if identity is None:
        return None
    source = _candidate_merged(candidate)
    promoted: dict[str, Any] = {
        "candidate_id": f"equipment:{identity}",
        "entity_type": "equipment",
        "similarity": float(candidate.get("similarity") or 0.0),
        "score": float(candidate.get("score") or candidate.get("similarity") or 0.0),
        "source": candidate.get("source") or "parent_equipment_projection",
    }
    for key in _EQUIPMENT_COPY_KEYS:
        value = source.get(key)
        if value not in (None, ""):
            promoted[key] = value

    metadata = candidate.get("metadata")
    if isinstance(metadata, Mapping):
        promoted["metadata"] = dict(metadata)
    return promoted


def _collapse_to_equipment_candidates(
    matches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    unpromotable: list[dict[str, Any]] = []
    for item in matches:
        entity_type = str(item.get("entity_type") or "").lower()
        if entity_type == "equipment":
            promoted = dict(item)
        else:
            promoted = _promote_candidate_to_equipment(item)
        if promoted is None:
            if entity_type == "equipment":
                unpromotable.append(dict(item))
            continue
        identity = _equipment_identity(promoted)
        if identity is None:
            unpromotable.append(promoted)
            continue
        current = groups.get(identity)
        if current is None or float(promoted.get("similarity") or 0.0) > float(
            current.get("similarity") or 0.0
        ):
            groups[identity] = promoted
    collapsed = list(groups.values()) + unpromotable
    return sorted(
        collapsed,
        key=lambda item: float(item.get("similarity") or 0.0),
        reverse=True,
    )


def collapse_to_equipment_candidates(
    matches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Public projection used when point recall spans several devices."""

    return _collapse_to_equipment_candidates(matches)


def postrank_entity_result(
    result: EntityLookupResult,
    *,
    query: str,
    required_level: str,
) -> EntityLookupResult:
    """Project Asset-MCP candidates to the workflow-required entity level.

    ``query`` is deliberately unused and retained only for API compatibility.  Older
    releases reparsed the user's free text here with regexes and could reject a
    perfectly correct Asset-MCP candidate (for example interpreting
    ``请全面介绍一下辅料事业部`` as a division name).  Natural-language constraints now
    have exactly one owner: Asset MCP.
    """

    del query
    if result.status not in {
        EntityStatus.MULTIPLE,
        EntityStatus.UNIQUE,
        EntityStatus.COLLECTION,
    } or not result.matches:
        return result

    if str(required_level or "none").lower() != "equipment":
        return result

    collapsed = _collapse_to_equipment_candidates(list(result.matches))
    if not collapsed:
        return result

    # A true collection request remains a collection.  Conversation is not allowed to
    # reinterpret it as a singular request based on wording.
    if result.status == EntityStatus.COLLECTION:
        status = EntityStatus.COLLECTION
        resolved = None
        need_disambiguation = False
        return_mode = result.return_mode
    else:
        status = EntityStatus.UNIQUE if len(collapsed) == 1 else EntityStatus.MULTIPLE
        resolved = collapsed[0] if status == EntityStatus.UNIQUE else None
        need_disambiguation = status == EntityStatus.MULTIPLE
        return_mode = "single" if status == EntityStatus.UNIQUE else result.return_mode

    top_similarity = max(
        (float(item.get("similarity") or item.get("score") or 0.0) for item in collapsed),
        default=0.0,
    )
    decision = dict(result.decision or {})
    decision["backend_level_projection_applied"] = True
    decision["backend_level_projection_reason"] = (
        "Conversation only projected real Asset-MCP candidates to equipment level; "
        "no user-text entity parsing was performed."
    )
    return result.model_copy(
        update={
            "status": status,
            "need_disambiguation": need_disambiguation,
            "matches": collapsed,
            "match_count": len(collapsed),
            "top_similarity": top_similarity,
            "resolved_entity": resolved,
            "resolution_source": (
                "backend_level_projection"
                if collapsed != result.matches
                else result.resolution_source
            ),
            "decision": decision,
            "return_mode": return_mode,
        }
    )
