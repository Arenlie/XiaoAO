from __future__ import annotations

import json
from typing import Any

from app.domain.enums import EntityStatus
from app.domain.phm_point_codes import enrich_phm_point_entity
from app.domain.exceptions import AppError
from app.integrations.dify.entity_identity import build_candidate_id
from app.integrations.dify.entity_scope import enrich_query_scope
from app.schemas.entity import EntityLookupResult


def _extract_outputs(payload: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    data = payload.get("data")
    if isinstance(data, dict):
        outputs = data.get("outputs")
        run_id = data.get("id") or payload.get("workflow_run_id")
        if isinstance(outputs, dict):
            return outputs, str(run_id) if run_id else None
    outputs = payload.get("outputs")
    if isinstance(outputs, dict):
        return outputs, str(payload.get("workflow_run_id") or "") or None
    raise AppError("ENTITY_RESULT_INVALID", "实体工作流未返回 outputs", 502)


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y"}:
            return True
        if normalized in {"0", "false", "no", "n", ""}:
            return False
    return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, result))



def _as_json_object(value: Any, *, field_name: str) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise AppError(
                "ENTITY_RESULT_INVALID", f"{field_name} 不是合法 JSON", 502
            ) from exc
    if not isinstance(value, dict):
        raise AppError("ENTITY_RESULT_INVALID", f"{field_name} 必须是对象", 502)
    return dict(value)


def _match_similarity(match: dict[str, Any]) -> float:
    for key in (
        "similarity",
        "final_score",
        "match_score",
        "rerank_score",
        "vector_score",
        "text_score",
        "score",
    ):
        if match.get(key) is not None:
            return _as_float(match.get(key))
    return 0.0


def parse_workflow_response(
    payload: dict[str, Any],
    *,
    profile_prior_available: bool = False,
) -> EntityLookupResult:
    """Parse the profile-aware entity Workflow's compact output contract.

    The Workflow returns `matches_json` as a JSON string. Counts and ambiguity are
    recomputed by the backend so malformed or contradictory Dify output cannot bypass
    the backend state machine.
    """

    outputs, run_id = _extract_outputs(payload)
    raw_status = str(outputs.get("status") or outputs.get("lookup_status") or "").upper()
    try:
        status = EntityStatus(raw_status)
    except ValueError as exc:
        raise AppError("ENTITY_RESULT_INVALID", f"未知实体检索状态: {raw_status}", 502) from exc

    raw_matches = outputs.get("matches")
    if raw_matches is None:
        raw_matches = outputs.get("matches_json", "[]")
    if isinstance(raw_matches, str):
        try:
            raw_matches = json.loads(raw_matches or "[]")
        except json.JSONDecodeError as exc:
            raise AppError("ENTITY_RESULT_INVALID", "matches_json 不是合法 JSON", 502) from exc
    if not isinstance(raw_matches, list) or not all(isinstance(item, dict) for item in raw_matches):
        raise AppError("ENTITY_RESULT_INVALID", "matches 必须是对象数组", 502)

    matches_by_id: dict[str, dict[str, Any]] = {}
    for raw_match in raw_matches:
        item = enrich_phm_point_entity(raw_match)
        item["similarity"] = _match_similarity(item)
        candidate_id = str(item.get("candidate_id") or build_candidate_id(item))
        item["candidate_id"] = candidate_id
        previous = matches_by_id.get(candidate_id)
        if previous is None or _match_similarity(item) > _match_similarity(previous):
            matches_by_id[candidate_id] = item
    matches = sorted(matches_by_id.values(), key=_match_similarity, reverse=True)
    match_count = len(matches)

    resolved_entity = outputs.get("resolved_entity")
    if isinstance(resolved_entity, str):
        try:
            resolved_entity = json.loads(resolved_entity) if resolved_entity else None
        except json.JSONDecodeError as exc:
            raise AppError("ENTITY_RESULT_INVALID", "resolved_entity 不是合法 JSON", 502) from exc
    if resolved_entity is not None and not isinstance(resolved_entity, dict):
        raise AppError("ENTITY_RESULT_INVALID", "resolved_entity 必须是对象", 502)
    if isinstance(resolved_entity, dict):
        resolved_entity = enrich_phm_point_entity(resolved_entity)

    if status == EntityStatus.UNIQUE:
        if match_count != 1:
            raise AppError("ENTITY_RESULT_INVALID", "UNIQUE 状态必须严格包含一个候选", 502)
        resolved_entity = resolved_entity or matches[0]
    elif status == EntityStatus.MULTIPLE:
        if match_count < 2:
            raise AppError("ENTITY_RESULT_INVALID", "MULTIPLE 状态至少需要两个候选", 502)
        resolved_entity = None
    elif status in {EntityStatus.NO_LOOKUP, EntityStatus.NOT_FOUND, EntityStatus.ERROR}:
        resolved_entity = None
        if match_count:
            raise AppError(
                "ENTITY_RESULT_INVALID",
                f"{status.value} 状态不应包含候选",
                502,
            )

    need_lookup = status != EntityStatus.NO_LOOKUP
    # Explicit output is accepted only when it agrees with the state-machine invariant.
    explicit_need_lookup = _as_bool(outputs.get("need_lookup"), need_lookup)
    if explicit_need_lookup != need_lookup:
        raise AppError("ENTITY_RESULT_INVALID", "need_lookup 与 status 不一致", 502)

    need_disambiguation = status == EntityStatus.MULTIPLE and match_count > 1
    explicit_disambiguation = _as_bool(
        outputs.get("need_disambiguation"), need_disambiguation
    )
    if explicit_disambiguation != need_disambiguation:
        raise AppError("ENTITY_RESULT_INVALID", "need_disambiguation 与候选状态不一致", 502)

    # Backend derives the effective top score from the normalized/sorted matches.
    # The Dify scalar is advisory only and cannot contradict the candidate array.
    top_similarity = _match_similarity(matches[0]) if matches else 0.0

    query_scope = _as_json_object(
        outputs.get("query_scope")
        if outputs.get("query_scope") is not None
        else outputs.get("query_scope_json"),
        field_name="query_scope_json",
    )
    entity_constraints = _as_json_object(
        outputs.get("entity_constraints")
        if outputs.get("entity_constraints") is not None
        else outputs.get("entity_constraints_json"),
        field_name="entity_constraints_json",
    )
    lookup_scope = str(outputs.get("lookup_scope") or "").strip() or None
    if lookup_scope:
        query_scope.setdefault("lookup_scope", lookup_scope)
    query_scope = enrich_query_scope(query_scope, resolved_entity)

    return EntityLookupResult(
        status=status,
        need_lookup=need_lookup,
        need_disambiguation=need_disambiguation,
        matches=matches,
        match_count=match_count,
        top_similarity=top_similarity,
        resolved_entity=resolved_entity,
        message=str(outputs.get("message") or outputs.get("display") or "") or None,
        workflow_run_id=run_id,
        profile_prior_used=(
            _as_bool(
                outputs.get("profile_prior_used"),
                False,
            )
            if profile_prior_available
            else False
        ),
        lookup_scope=lookup_scope,
        resolution_source=str(outputs.get("resolution_source") or "").strip() or None,
        query_scope=query_scope,
        entity_constraints=entity_constraints,
    )
