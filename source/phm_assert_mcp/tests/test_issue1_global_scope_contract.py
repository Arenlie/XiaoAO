from types import SimpleNamespace

import pytest

from app.asset_query_contract import AssetQuery
from app.services.asset_collection_engine import AssetCollectionEngine


def test_shared_catalog_scope_is_a_valid_internal_asset_query_scope():
    query = AssetQuery.model_validate(
        {
            "scope": {"scope_type": "shared_catalog"},
            "predicate": {
                "field": "equipment_class",
                "operator": "is",
                "value": "水泵",
            },
            "operation": "count",
        }
    )
    assert query.scope.scope_type == "shared_catalog"
    assert query.scope.target_entity_level == "equipment"
    assert query.scope.model_dump() == {
        "scope_type": "shared_catalog",
        "target_entity_level": "equipment",
    }


@pytest.mark.asyncio
async def test_shared_catalog_scope_compiles_to_whole_equipment_catalog_without_fake_root_id():
    engine = AssetCollectionEngine(None, SimpleNamespace(asset_catalog_table="public.asset_catalog"))
    scope_name, scope_sql, args = await engine._scope(
        object(), {"scope_type": "shared_catalog", "target_entity_level": "equipment"}
    )
    assert scope_name == "全部资产"
    assert scope_sql == "TRUE"
    assert args == []
