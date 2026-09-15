from __future__ import annotations

import hashlib
import json
from typing import Any


def build_candidate_id(candidate: dict[str, Any]) -> str:
    """Build a stable, collision-resistant candidate id for frontend selection.

    The profile-aware Workflow intentionally returns only normalized business fields.
    Point numbers are only unique within equipment. Prefer the catalog key, then
    a source/equipment/point composite; never identify a point by point_no alone.
    """

    entity_type = str(candidate.get("entity_type") or candidate.get("type") or "entity").lower()
    if candidate.get("point_no"):
        entity_type = "point"
    if entity_type == "point":
        metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
        namespace = candidate.get("data_source") or metadata.get("data_source") or "asset"
        natural_key = (
            candidate.get("entity_key")
            or candidate.get("id")
        )
        if not natural_key and candidate.get("point_no") and candidate.get("equip_no"):
            natural_key = json.dumps([namespace, candidate["equip_no"],
                                     candidate["point_no"]], ensure_ascii=False, separators=(",", ":"))
        elif natural_key and namespace != "asset":
            natural_key = json.dumps([namespace, str(natural_key)], ensure_ascii=False, separators=(",", ":"))
    elif entity_type == "equipment":
        natural_key = (
            candidate.get("equip_no")
            or candidate.get("entity_key")
            or candidate.get("id")
        )
    elif entity_type == "area":
        metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
        natural_key = (
            candidate.get("entity_key")
            or candidate.get("space_id")
            or candidate.get("space_code")
            or candidate.get("space_number")
            or metadata.get("space_id")
            or metadata.get("space_code")
            or metadata.get("space_number")
            or candidate.get("area_key")
            or candidate.get("area_name")
            or candidate.get("region_name")
            or candidate.get("workshop_name")
            or candidate.get("line_name")
            or candidate.get("id")
        )
    else:
        natural_key = (
            candidate.get("entity_key")
            or candidate.get("point_no")
            or candidate.get("equip_no")
            or candidate.get("id")
        )

    if natural_key:
        return f"{entity_type}:{str(natural_key).strip()}"

    canonical = {
        key: candidate.get(key)
        for key in (
            "entity_type",
            "equip_no",
            "equip_name",
            "point_no",
            "point_name",
            "area_key",
            "area_name",
            "region_name",
            "workshop_name",
            "line_name",
        )
    }
    digest = hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:24]
    return f"{entity_type}:sha256-{digest}"
