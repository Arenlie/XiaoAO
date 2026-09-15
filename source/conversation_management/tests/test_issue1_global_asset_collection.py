from app.asset_collection_scope import is_unscoped_new_asset_collection
from app.orchestration.entity_dependency import identity_dependency
from app.services.asset_collections import build_query
from app.tools.phm_asset_context import asset_identity_available
from app.asset_query_contract import TOOL_ID


def global_collection_state():
    return {
        "query": "现在一共有多少台水泵？",
        # Simulate stale context from an older topic. It must not scope this new query.
        "active_entity": {
            "entity_type": "equipment",
            "equip_no": "OLD-PUMP",
            "space_id": "429",
            "equip_name": "水泵",
        },
        "business_intent": {
            "asset_query": {
                "active": True,
                "operation": "count",
                "reference_mode": "new",
                "predicate": {
                    "field": "equipment_class",
                    "operator": "is",
                    "value": "水泵",
                    "include_descendants": True,
                },
            },
            "asset_semantics": {
                "needs_asset_lookup": True,
                "equipment_type": {"raw_text": "水泵", "retrieval_text": "水泵"},
                "reference_target_level": "none",
            },
            "goal_frame": {
                "goal": "count_water_pumps",
                "anchor_entity_level": "any",
                "target_entity_level": "equipment",
                "evidence_types": ["asset_count"],
                "can_answer_from_context": False,
            },
        },
    }


def test_unscoped_collection_does_not_require_singular_entity_resolution():
    state = global_collection_state()
    assert is_unscoped_new_asset_collection(state)
    dependency = identity_dependency(state)
    assert dependency["required"] is False
    assert dependency["source"] == "shared_catalog_collection"
    assert asset_identity_available(state, TOOL_ID) is True


def test_unscoped_collection_builds_shared_catalog_scope_and_ignores_stale_active_entity():
    query = build_query(global_collection_state())
    assert query["scope"] == {
        "scope_type": "shared_catalog",
        "target_entity_level": "equipment",
    }
    assert query["predicate"]["value"] == "水泵"
    assert "root_space_id" not in query["scope"]


def test_explicit_area_collection_still_requires_area_resolution():
    state = global_collection_state()
    state["query"] = "总部钢铁现在有多少台水泵？"
    state["business_intent"]["asset_semantics"]["area"] = {
        "raw_text": "总部钢铁",
        "retrieval_text": "总部钢铁",
    }
    state["business_intent"]["goal_frame"]["anchor_entity_level"] = "area"
    assert not is_unscoped_new_asset_collection(state)
    dependency = identity_dependency(state)
    assert dependency["required"] is True
    assert dependency["level"] == "area"


def test_referential_collection_is_not_promoted_to_global_scope():
    state = global_collection_state()
    state["query"] = "这个区域现在有多少台水泵？"
    state["business_intent"]["asset_semantics"]["reference_target_level"] = "space"
    assert not is_unscoped_new_asset_collection(state)
    assert identity_dependency(state)["required"] is True


def test_single_equipment_health_query_still_requires_entity_resolution():
    state = {
        "query": "水泵的健康度怎么样？",
        "business_intent": {
            "asset_query": {"active": False},
            "asset_semantics": {
                "needs_asset_lookup": True,
                "equipment_type": {"raw_text": "水泵", "retrieval_text": "水泵"},
                "reference_target_level": "none",
            },
            "goal_frame": {
                "goal": "query_health",
                "anchor_entity_level": "equipment",
                "target_entity_level": "equipment",
                "evidence_types": ["health"],
                "can_answer_from_context": False,
            },
        },
    }
    assert not is_unscoped_new_asset_collection(state)
    dependency = identity_dependency(state)
    assert dependency["required"] is True
    assert dependency["level"] == "equipment"
