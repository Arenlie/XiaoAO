"""Exact code constraints exercised against an explicit disposable PostgreSQL DSN."""
import pytest
from app.repositories.catalog_repository import CatalogRepository
from test_v150_collection_sql import env, DSN

pytestmark = pytest.mark.skipif(not DSN, reason='explicit disposable PostgreSQL DSN required')


async def test_same_point_number_has_nine_real_keys_and_codes_are_bound_parameters(env):
    _, db, settings, *_ = env
    for i in range(9):
        await db.execute('''INSERT INTO public.phm_test_asset_catalog(entity_type,entity_key,equip_no,point_no,display_name,metadata)
            VALUES('point',$1,$2,'GY000','输入轴测点','{}'::jsonb)''', f'point:{i}', f'DEV-{i}')
    repo = CatalogRepository(db, settings)
    rows = await repo.lookup_code_tokens(['GY000'])
    assert len(rows) == 9 and len({r.entity_key for r in rows}) == 9
    rows = await repo.exact_code(equip_no='DEV-8', point_no='GY000', scope='point')
    assert len(rows) == 1 and rows[0].entity_key == 'point:8'
    assert await repo.lookup_code_tokens(["GY000' OR 1=1 --"]) == []
    assert len(await repo.lookup_code_tokens(['GY000'])) == 9
