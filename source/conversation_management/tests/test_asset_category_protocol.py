import copy

import pytest

from app.asset_query_contract import AssetQuery, QueryError, normalize_tree, signature
from app.services.asset_collections import validate_response


def _query():
    return {
        "schema_version": "2.0",
        "scope": {"root_space_id": "225", "target_entity_level": "equipment", "recursive": True},
        "predicate": {
            "field": "equipment_class",
            "operator": "is",
            "value": "轧机",
            "include_descendants": True,
        },
        "operation": "count",
        "page_size": 1000,
        "freshness": "current",
    }


def _trace():
    return {
        "field": "equipment_class",
        "input_term": "轧机",
        "strict_error": "CATEGORY_UNSUPPORTED",
        "resolution_source": "llm_temporary_hierarchy",
        "hierarchy_persisted": False,
        "candidate_source": "reviewed_catalog_tags",
        "candidate_total": 57,
        "candidate_sent": 57,
        "selected_tag_codes": ["equipment.rolling_mill", "equipment.skin_pass_mill"],
        "selected_tags": [
            {"tag_code": "equipment.rolling_mill", "tag_name": "轧机/轧制设备"},
            {"tag_code": "equipment.skin_pass_mill", "tag_name": "平整机"},
        ],
        "llm_relations": [
            {"tag_code": "equipment.rolling_mill", "tag_name": "轧机/轧制设备", "relation": "SAME", "selected": True},
            {"tag_code": "equipment.skin_pass_mill", "tag_name": "平整机", "relation": "CHILD", "selected": True},
            {"tag_code": "equipment.rolling_mill_motor", "tag_name": "轧机主电机", "relation": "RELATED", "selected": False},
        ],
        "coverage_complete": False,
        "reason": "临时语义层级推断",
    }


def _payload(query):
    q = AssetQuery.model_validate(query)
    trace = _trace()
    effective = {
        "any": [
            {"field": "equipment_class", "operator": "is", "category_id": "equipment.rolling_mill", "include_descendants": False},
            {"field": "equipment_class", "operator": "is", "category_id": "equipment.skin_pass_mill", "include_descendants": False},
        ]
    }
    return {
        "success": True,
        "status": "PARTIAL",
        "schema_version": "2.0",
        "request_signature": signature(q.model_dump(mode="json")),
        "operation": q.operation,
        "group_by": q.group_by,
        "count": 12,
        "unknown_count": 0,
        "result_complete": False,
        "criteria": {
            "scope": q.scope.model_dump(),
            "requested_predicate": normalize_tree(q.predicate),
            "predicate": normalize_tree(effective),
            "category_resolution": [trace],
        },
        "category_resolution": [trace],
    }


def test_validate_response_accepts_audited_category_fallback_rewrite():
    query = _query()
    validate_response(query, _payload(query))


def test_validate_response_rejects_effective_predicate_not_approved_by_trace():
    query = _query()
    payload = _payload(query)
    payload["criteria"]["predicate"]["any"].append({
        "field": "equipment_class",
        "operator": "is",
        "category_id": "equipment.rolling_mill_motor",
        "include_descendants": False,
    })
    with pytest.raises(QueryError) as exc:
        validate_response(query, payload)
    assert exc.value.code == "QUERY_RESULT_MISMATCH"


def test_validate_response_rejects_modified_requested_predicate():
    query = _query()
    payload = _payload(query)
    payload["criteria"]["requested_predicate"]["value"] = "电机"
    with pytest.raises(QueryError) as exc:
        validate_response(query, payload)
    assert exc.value.code == "QUERY_RESULT_MISMATCH"


def test_validate_response_rejects_related_tag_even_when_selected():
    query = _query()
    payload = _payload(query)
    payload["criteria"]["category_resolution"][0]["selected_tag_codes"] = ["equipment.rolling_mill_motor"]
    payload["criteria"]["category_resolution"][0]["selected_tags"] = [
        {"tag_code": "equipment.rolling_mill_motor", "tag_name": "轧机主电机"}
    ]
    payload["criteria"]["category_resolution"][0]["llm_relations"][-1]["selected"] = True
    payload["criteria"]["predicate"] = {
        "field": "equipment_class",
        "operator": "is",
        "category_id": "equipment.rolling_mill_motor",
        "include_descendants": False,
    }
    payload["category_resolution"] = copy.deepcopy(payload["criteria"]["category_resolution"])
    with pytest.raises(QueryError) as exc:
        validate_response(query, payload)
    assert exc.value.code == "QUERY_RESULT_MISMATCH"


def test_validate_response_keeps_original_exact_predicate_contract():
    query = _query()
    q = AssetQuery.model_validate(query)
    payload = {
        "success": True,
        "schema_version": "2.0",
        "request_signature": signature(q.model_dump(mode="json")),
        "operation": q.operation,
        "group_by": q.group_by,
        "count": 1,
        "unknown_count": 0,
        "result_complete": True,
        "criteria": {"scope": q.scope.model_dump(), "predicate": normalize_tree(q.predicate)},
    }
    validate_response(query, payload)
