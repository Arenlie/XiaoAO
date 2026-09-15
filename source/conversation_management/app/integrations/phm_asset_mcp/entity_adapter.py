from __future__ import annotations

from typing import Any

from app.domain.enums import EntityStatus
from app.domain.phm_point_codes import enrich_phm_point_entity
from app.integrations.dify.entity_identity import build_candidate_id
from app.integrations.dify.entity_scope import enrich_query_scope
from app.schemas.entity import EntityLookupResult
from app.integrations.phm_asset_mcp.errors import PhmAssetMcpError


_STATUS_MAP = {
    "NO_LOOKUP": EntityStatus.NO_LOOKUP,
    "RESOLVED": EntityStatus.UNIQUE,
    "NEEDS_DISAMBIGUATION": EntityStatus.MULTIPLE,
    "ENTITY_NOT_FOUND": EntityStatus.NOT_FOUND,
    "RESOLVED_COLLECTION": EntityStatus.COLLECTION,
}


def _normalize_match(value: dict[str, Any]) -> dict[str, Any]:
    item = enrich_phm_point_entity(dict(value))
    item["candidate_id"] = build_candidate_id(item)
    if item.get("similarity") is None:
        item["similarity"] = item.get("confidence") or item.get("score") or 0.0
    return item


def adapt_asset_resolution(payload: dict[str, Any]) -> EntityLookupResult:
    """Convert the Asset MCP 0.3 resolver contract to conversation state."""

    raw_status = str(payload.get("status") or "").upper()
    if raw_status not in _STATUS_MAP:
        raise PhmAssetMcpError("PHM_ASSET_MCP_INVALID_RESULT",
                               f"resolve_entity: unknown business status {raw_status!r}")
    status = _STATUS_MAP[raw_status]
    entity = payload.get("entity")
    resolved_entity = _normalize_match(entity) if isinstance(entity, dict) and entity else None
    if status == EntityStatus.UNIQUE and not resolved_entity:
        raise PhmAssetMcpError("PHM_ASSET_MCP_INVALID_RESULT",
                               "resolve_entity: RESOLVED without a real entity")

    raw_matches = payload.get("resolved_entities") if status == EntityStatus.COLLECTION else payload.get("matches")
    matches = [
        _normalize_match(item)
        for item in (raw_matches or [])
        if isinstance(item, dict)
    ]
    if status == EntityStatus.UNIQUE and resolved_entity:
        matches = [resolved_entity]
    if status in {EntityStatus.NO_LOOKUP, EntityStatus.NOT_FOUND, EntityStatus.ERROR}:
        matches = []
        resolved_entity = None

    decision = payload.get("decision") if isinstance(payload.get("decision"), dict) else {}
    query_scope = payload.get("query_scope") if isinstance(payload.get("query_scope"), dict) else {}
    query_scope = enrich_query_scope(query_scope, resolved_entity)
    if isinstance(payload.get("_performance_trace"), dict):
        query_scope["_performance_trace"] = payload.get("_performance_trace")
    query_scope["entity_resolution_decision"] = dict(decision)
    query_scope["return_mode"] = str(payload.get("return_mode") or decision.get("return_mode") or "single")

    top_similarity = 0.0
    if matches:
        try:
            top_similarity = float(matches[0].get("similarity") or matches[0].get("score") or 0.0)
        except (TypeError, ValueError):
            top_similarity = 0.0

    return EntityLookupResult(
        status=status,
        need_lookup=bool(payload.get("need_lookup", decision.get("need_lookup", status != EntityStatus.NO_LOOKUP))),
        need_disambiguation=status == EntityStatus.MULTIPLE,
        matches=matches,
        match_count=len(matches),
        top_similarity=max(0.0, min(1.0, top_similarity)),
        resolved_entity=resolved_entity,
        message=str(payload.get("message") or "") or None,
        workflow_run_id=None,
        profile_prior_used=bool(payload.get("profile_prior_used", False)),
        lookup_scope=str(payload.get("lookup_scope") or decision.get("lookup_scope") or "") or None,
        resolution_source=str(payload.get("resolution_source") or "phm_asset_mcp") or None,
        query_scope=query_scope,
        entity_constraints=(
            dict(payload.get("entity_constraints"))
            if isinstance(payload.get("entity_constraints"), dict)
            else {}
        ),
        decision=dict(decision),
        return_mode=str(payload.get("return_mode") or decision.get("return_mode") or "single"),
        query_fingerprint=str(payload.get("query_fingerprint") or decision.get("query_fingerprint") or "") or None,
    )
