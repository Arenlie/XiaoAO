"""Deterministic scope policy for asset collection queries.

This module separates a collection predicate (for example equipment_type=水泵)
from a singular asset identity.  It deliberately consumes only structured task
semantics; no natural-language keyword rules are allowed here.
"""
from __future__ import annotations

from typing import Any, Mapping


def _constraint_has_raw(value: Any) -> bool:
    return isinstance(value, Mapping) and bool(str(value.get("raw_text") or "").strip())


def is_unscoped_new_asset_collection(state: Mapping[str, Any]) -> bool:
    """Whether this turn is a new equipment collection over the shared catalog.

    A category/name predicate such as ``equipment_type=水泵`` is not a singular
    equipment identity.  The query is global only when Task Understanding already
    declared an asset collection and did not provide or reference a concrete root
    area/equipment/point.  Explicit/referential roots must still go through the
    normal entity-resolution path.
    """

    intent = state.get("business_intent") if isinstance(state.get("business_intent"), Mapping) else {}
    asset_query = intent.get("asset_query") if isinstance(intent.get("asset_query"), Mapping) else {}
    if not asset_query or asset_query.get("active") is not True:
        return False
    if str(asset_query.get("reference_mode") or "new").strip().lower() != "new":
        return False

    semantics = intent.get("asset_semantics") if isinstance(intent.get("asset_semantics"), Mapping) else {}
    reference = str(semantics.get("reference_target_level") or "none").strip().lower()
    if reference not in {"", "none"}:
        return False

    # These fields identify a concrete query root.  equipment_type is intentionally
    # absent: it is a collection predicate when asset_query.active=true.
    for field in ("area", "equipment", "equip_no", "point", "point_no"):
        if _constraint_has_raw(semantics.get(field)):
            return False
    return True
