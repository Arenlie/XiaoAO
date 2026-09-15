from types import SimpleNamespace

import pytest

from app.asset_query_contract import QueryError
from app.services.asset_collection_engine import AssetCollectionEngine
from app.services.asset_taxonomy import Taxonomy, compile_predicate


class FakeLlm:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def resolve_equipment_category_fallback(self, **kwargs):
        self.calls.append(kwargs)
        return self.payload


def taxonomy_without_parent_labels():
    return Taxonomy([
        {"code": "equipment.rough_rolling_mill", "name": "粗轧机"},
        {"code": "equipment.finish_rolling_mill", "name": "精轧机"},
        {"code": "equipment.rolling_mill_motor", "name": "轧机主电机"},
        {"code": "equipment.pump", "name": "水泵"},
    ])


def engine(llm):
    settings = SimpleNamespace(asset_catalog_table="public.entity_search_catalog")
    return AssetCollectionEngine(None, settings, llm)


def test_fallback_candidates_are_real_reviewed_tags_and_lexically_prioritized():
    taxonomy = taxonomy_without_parent_labels()
    candidates, total = taxonomy.fallback_candidates("equipment_class", "轧机", limit=50)
    assert total == 4
    assert {item["tag_code"] for item in candidates} == {
        "equipment.rough_rolling_mill",
        "equipment.finish_rolling_mill",
        "equipment.rolling_mill_motor",
        "equipment.pump",
    }
    assert candidates[0]["lexical_score"] >= candidates[-1]["lexical_score"]
    assert candidates[0]["tag_name"] in {"粗轧机", "精轧机", "轧机主电机"}


@pytest.mark.asyncio
async def test_unsupported_parent_term_is_rewritten_to_real_child_tag_union():
    taxonomy = taxonomy_without_parent_labels()
    llm = FakeLlm({
        "selected_tag_codes": ["equipment.rough_rolling_mill", "equipment.finish_rolling_mill"],
        "items": [
            {"tag_code": "equipment.rough_rolling_mill", "tag_name": "粗轧机", "relation": "CHILD", "selected": True, "confidence": 0.99},
            {"tag_code": "equipment.finish_rolling_mill", "tag_name": "精轧机", "relation": "CHILD", "selected": True, "confidence": 0.99},
            {"tag_code": "equipment.rolling_mill_motor", "tag_name": "轧机主电机", "relation": "RELATED", "selected": False, "confidence": 0.99},
        ],
        "coverage_complete": True,
        "reason": "粗轧机、精轧机是轧机子类，轧机主电机只是相关设备。",
    })
    predicate = {"field": "equipment_class", "operator": "is", "value": "轧机", "include_descendants": True}

    # Strict resolution still fails first; no hidden alias/parent rule was added.
    with pytest.raises(QueryError) as exc:
        compile_predicate(predicate, taxonomy, [], normalized=[])
    assert exc.value.code == "CATEGORY_UNSUPPORTED"

    rewritten, traces = await engine(llm)._resolve_equipment_class_fallback(predicate, taxonomy)
    assert rewritten == {
        "any": [
            {"field": "equipment_class", "operator": "is", "category_id": "equipment.rough_rolling_mill", "include_descendants": False},
            {"field": "equipment_class", "operator": "is", "category_id": "equipment.finish_rolling_mill", "include_descendants": False},
        ]
    }
    assert traces[0]["hierarchy_persisted"] is False
    assert traces[0]["candidate_source"] == "reviewed_catalog_tags"
    assert traces[0]["coverage_complete"] is True
    assert llm.calls and llm.calls[0]["input_term"] == "轧机"


@pytest.mark.asyncio
async def test_llm_cannot_invent_tag_or_use_related_equipment_as_child():
    taxonomy = taxonomy_without_parent_labels()
    llm = FakeLlm({
        "selected_tag_codes": ["equipment.not_real"],
        "items": [
            {"tag_code": "equipment.rolling_mill_motor", "tag_name": "轧机主电机", "relation": "RELATED", "selected": True, "confidence": 1.0},
        ],
        "coverage_complete": True,
        "reason": "bad response",
    })
    predicate = {"field": "equipment_class", "operator": "is", "value": "轧机"}
    with pytest.raises(QueryError) as exc:
        await engine(llm)._resolve_equipment_class_fallback(predicate, taxonomy)
    assert exc.value.code == "CATEGORY_UNSUPPORTED"

@pytest.mark.asyncio
async def test_compile_gate_retries_same_predicate_with_real_category_ids():
    taxonomy = taxonomy_without_parent_labels()
    llm = FakeLlm({
        "selected_tag_codes": ["equipment.rough_rolling_mill", "equipment.finish_rolling_mill"],
        "items": [
            {"tag_code": "equipment.rough_rolling_mill", "tag_name": "粗轧机", "relation": "CHILD", "selected": True, "confidence": 0.98},
            {"tag_code": "equipment.finish_rolling_mill", "tag_name": "精轧机", "relation": "CHILD", "selected": True, "confidence": 0.98},
        ],
        "coverage_complete": True,
        "reason": "两类共同覆盖轧机。",
    })
    predicate = {"all": [{"field": "equipment_class", "operator": "is", "value": "轧机", "include_descendants": True}]}
    condition, args, normalized, effective, traces = await engine(llm)._compile_with_category_fallback(
        predicate, taxonomy, ["153/"]
    )
    assert " OR " in condition
    assert args[0] == "153/"
    tag_lists = [tuple(v) for v in args[1:] if isinstance(v, list) and v and str(v[0]).startswith("equipment.")]
    assert ("equipment.rough_rolling_mill",) in tag_lists
    assert ("equipment.finish_rolling_mill",) in tag_lists
    assert effective["all"][0]["any"][0]["category_id"] == "equipment.rough_rolling_mill"
    assert traces[0]["selected_tag_codes"] == ["equipment.rough_rolling_mill", "equipment.finish_rolling_mill"]
    assert any("category_resolution" in item for item in normalized)
