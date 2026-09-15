"""Integration suite. Requires an EXPLICIT disposable PostgreSQL DSN.

PHM_TEST_PG_DSN=postgresql://... python -m pytest tests/test_v150_collection_sql.py
Only public.phm_test_asset_catalog, phm_test_spaces and phm_asset_query_* are touched.
Use a disposable database: this suite clears the query snapshot tables.
"""
import json
import os
from uuid import uuid4

import pytest
import pytest_asyncio

from app.config import Settings
from app.db import DatabaseManager, _init_connection
from app.asset_query_contract import QueryError, sign_context
from app.services.asset_collection_engine import AssetCollectionEngine

DSN = os.environ.get("PHM_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="explicit disposable PostgreSQL DSN required")


@pytest_asyncio.fixture
async def env(tmp_path):
    import asyncpg
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=1, init=_init_connection)
    settings = Settings.model_construct(asset_catalog_table="public.phm_test_asset_catalog",
        asset_unified_query_enabled=True, asset_query_context_secret="test-only-"+"x"*48)
    db = DatabaseManager(settings)
    db._pool = pool
    engine = AssetCollectionEngine(db, settings)
    await engine.migrate()
    await db.execute("TRUNCATE public.phm_asset_query_runs CASCADE")
    await db.execute("""DROP TABLE IF EXISTS public.phm_test_asset_catalog;
        CREATE TABLE public.phm_test_asset_catalog(entity_type text,entity_key text,equip_no text,
        point_no text,display_name text,search_text text,metadata jsonb,tag_codes text[],tag_names text[],semantic_review_status text);
        CREATE OR REPLACE FUNCTION public.phm_test_normalize(t text) RETURNS text LANGUAGE sql IMMUTABLE AS $$SELECT lower(replace(t,' ',''))$$;""")
    settings.asset_normalize_function = "public.phm_test_normalize"
    async def insert(kind, key, name, md, tags=None, names=None, reviewed="APPROVED", search=""):
        await db.execute("""INSERT INTO public.phm_test_asset_catalog VALUES($1,$2,$3,NULL,$4,$5,$6,$7,$8,$9)""",
            kind,key,key if kind=="equipment" else None,name,search,md,tags,names,reviewed)
    await insert("area","153","总部钢铁",{"space_id":"153","space_link":"153","leaf_space_name":"总部钢铁"})
    await insert("area","231","粗轧区",{"space_id":"231","space_link":"153/231","leaf_space_name":"粗轧区"})
    for i in range(115):
        await insert("equipment",f"P{i:03}",f"水泵{i}" if i<71 else f"冷却循环机{i}",
            {"equip_name":f"水泵{i}" if i<71 else f"冷却循环机{i}","space_id":"200","space_link":"153/200",
             "equip_type":"PUMP","classification_complete_dimensions":["equipment_class"]},
            ["equipment.pump.water"],["水泵"])
    for i in range(26):
        await insert("equipment",f"S{i:03}",f"水泵辅助开关{i}",{"space_id":"200","space_link":"153/200",
            "equip_type":"SWITCH","classification_complete_dimensions":["equipment_class"]},["equipment.switch"],["开关"])
    for i in range(6):
        await insert("equipment",f"R{i:03}",f"{i+1}架粗轧机",{"space_id":"231","space_link":"153/231",
            "classification_complete_dimensions":["equipment_class"]},["equipment.rolling_mill"],["轧机"])
    owner={"subject":"owner-A","conversation_id":"conversation-A","branch_id":"branch-A","task_id":"task-A","call_id":"call-A","shared_catalog":True}
    async def query(q, who=None):
        claims={**owner,"call_id":str(uuid4()),**(who or {})}
        return await engine.query(q, sign_context(settings.asset_query_context_secret,claims,q))
    yield engine,db,settings,query,insert,owner,tmp_path
    pool.terminate()  # disposable fixture; avoids socket-server Terminate handshake differences


def current(predicate=None, operation="count", **kw):
    return {"scope":{"root_space_id":"153"},"predicate":predicate,"operation":operation,**kw}


def water():
    return {"field":"equipment_class","operator":"is","value":"水泵"}


def follow(query_id, operation="group", **kw):
    return {"reference":{"query_id":query_id,"mode":"same_set"},"freshness":"referenced_snapshot", "operation":operation,
            **({"group_by":"equipment_class"} if operation=="group" else {}),**kw}


@pytest.mark.asyncio
async def test_115_categories_97_names_and_original_followup(env):
    engine,db,s,q,insert,owner,path=env
    category=await q(current(water()))
    name=await q(current({"field":"equipment_name","operator":"contains","value":"水泵"}))
    assert (category["count"],name["count"])==(115,97)
    groups=await q(follow(category["query_id"]))
    assert groups["count"]==115 and groups["groups"]==[{"name":"水泵","count":115}]
    assert groups["criteria_signature"]==category["criteria_signature"]
    listed=await q(follow(category["query_id"],"list"))
    assert len(listed["devices"])==115 and listed["count"]==115


@pytest.mark.asyncio
async def test_shared_catalog_scope_counts_all_catalog_equipment_without_resolving_a_fake_root(env):
    engine,db,s,q,insert,*_=env
    await insert("equipment","OUT-PUMP","范围外水泵",{
        "equip_name":"范围外水泵","space_id":"999","space_link":"999",
        "equip_type":"PUMP","classification_complete_dimensions":["equipment_class"]},
        ["equipment.pump.water"],["水泵"])
    scoped=await q(current(water()))
    global_result=await q({"scope":{"scope_type":"shared_catalog"},"predicate":water(),"operation":"count"})
    assert scoped["count"]==115
    assert global_result["count"]==116
    assert global_result["scope_name"]=="全部资产"
    assert global_result["criteria"]["scope"]=={
        "scope_type":"shared_catalog","target_entity_level":"equipment"}


@pytest.mark.asyncio
async def test_generic_device_is_level_not_heat_class(env):
    *_,q,insert,owner,path=env
    data=await q({"scope":{"root_space_id":"231"},"predicate":{"field":"equipment_class","operator":"is","value":"设备"}})
    assert data["count"]==6 and len(data["devices"])==6


@pytest.mark.asyncio
async def test_snapshot_survives_catalog_change_refresh_reads_current(env):
    engine,db,s,q,*_=env
    first=await q(current(water()))
    await db.execute("DELETE FROM public.phm_test_asset_catalog WHERE equip_no='P000'")
    original=await q(follow(first["query_id"]))
    refreshed=await q({"reference":{"query_id":first["query_id"],"mode":"refresh"},"operation":"count"})
    assert (original["count"],refreshed["count"])==(115,114)
    assert refreshed["query_id"] != first["query_id"]


@pytest.mark.asyncio
async def test_unknown_not_zero_and_not_unknown_is_unknown(env):
    engine,db,s,q,insert,*_=env
    await insert("equipment","UNK","未知资料",{"space_link":"153/200"},None,None,"PENDING")
    result=await q(current(water()))
    excluded=await q(current({"not":water()}))
    assert result["count"]==115 and result["unknown_count"]==1 and not result["result_complete"]
    assert excluded["count"]==32 and excluded["unknown_count"]==1


@pytest.mark.asyncio
async def test_refine_original_collection_excludes_name_only_auxiliary(env):
    engine,db,s,q,*_=env
    first=await q(current(water()))
    result=await q({"reference":{"query_id":first["query_id"],"mode":"refine_set"},"freshness":"referenced_snapshot",
                   "predicate":{"field":"equipment_name","operator":"contains","value":"水泵"},"operation":"count"})
    assert result["count"]==71


@pytest.mark.asyncio
async def test_or_not_and_literal_wildcards(env):
    engine,db,s,q,insert,*_=env
    await insert("equipment","LITERAL","泵%_\\站",{"space_link":"153/200"})
    result=await q(current({"field":"equipment_name","operator":"contains","value":"%_\\"}))
    assert result["count"]==1
    both=await q(current({"any":[water(),{"field":"equipment_class","operator":"is","value":"开关"}]}))
    assert both["count"]==141
    neither=await q(current({"not":{"any":[water(),{"field":"equipment_class","operator":"is","value":"开关"}]}}))
    assert neither["count"]==6 and neither["unknown_count"]==1


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_change",[{"subject":"other"},{"conversation_id":"other"},{"branch_id":"other"}])
async def test_private_reference_isolation(env,owner_change):
    engine,db,s,q,*_=env
    first=await q(current(water()))
    with pytest.raises(QueryError,match="当前对话"):
        await q(follow(first["query_id"]),owner_change)


@pytest.mark.asyncio
async def test_expired_snapshot_never_recomputed_as_historical(env):
    engine,db,s,q,*_=env
    first=await q(current(water()))
    await db.execute("UPDATE public.phm_asset_query_runs SET expires_at=now()-interval '1 second'")
    with pytest.raises(QueryError,match="已过期"):
        await q(follow(first["query_id"]))


@pytest.mark.asyncio
async def test_paging_no_gaps_and_cursor_tampering(env):
    engine,db,s,q,*_=env
    query=current(water(),"list",page_size=50)
    first=await q(query)
    second=await q({**query,"cursor":first["next_cursor"]})
    third=await q({**query,"cursor":second["next_cursor"]})
    assert [len(x["devices"]) for x in (first,second,third)]==[50,50,15]
    assert len({d["equip_no"] for page in (first,second,third) for d in page["devices"]})==115
    assert third["page_complete"]
    with pytest.raises(QueryError):
        await q({**query,"page_size":10,"cursor":first["next_cursor"]})


@pytest.mark.asyncio
async def test_authorized_fork_only_inherits_actual_ancestor(env):
    engine,db,s,q,*_=env
    first=await q(current(water()))
    result=await q(follow(first["query_id"]),{"branch_id":"fork","allowed_query_ids":[first["query_id"]]})
    assert result["count"]==115
    with pytest.raises(QueryError):
        await q(follow(first["query_id"]),{"subject":"other","branch_id":"fork","allowed_query_ids":[first["query_id"]]})


@pytest.mark.asyncio
async def test_missing_root_conflicting_identity_and_unknown_class(env):
    engine,db,s,q,insert,*_=env
    with pytest.raises(QueryError):
        await q({"scope":{"root_space_id":"not-real"}})
    with pytest.raises(QueryError,match="已审核"):
        await q(current({"field":"equipment_class","operator":"is","value":"未定义设备种类"}))
    await insert("equipment","P000","错误重名资料",{"space_link":"153/200"})
    with pytest.raises(QueryError,match="互相矛盾"):
        await q(current())


@pytest.mark.asyncio
async def test_capacity_preserves_exact_count_and_never_fake_full_list(env):
    engine,db,s,q,*_=env
    s.asset_query_snapshot_max_members=10
    result=await q(current(water()))
    assert result["count"]==115 and result["snapshot_available"] is False
    listed=await q(current(water(),"list"))
    assert listed["count"]==115 and listed["result_complete"] is False and not listed["devices"]


@pytest.mark.asyncio
async def test_retry_idempotency_and_changed_body_rejected(env):
    engine,db,s,q,insert,owner,*_=env
    request=current(water())
    token=sign_context(s.asset_query_context_secret,owner,request)
    first=await engine.query(request,token)
    second=await engine.query(request,token)
    assert first["query_id"]==second["query_id"]
    changed=current(None)
    with pytest.raises(QueryError):
        await engine.query(changed,sign_context(s.asset_query_context_secret,owner,changed))
    with pytest.raises(QueryError):
        await engine.query(changed,token)


@pytest.mark.asyncio
async def test_legacy_adapters_use_same_sql_and_preserve_device_fields(env):
    from app.schemas.equipment import QueryDevicesRequest
    from app.schemas.collection import QueryScopeCollectionRequest
    engine,db,s,q,*_=env
    legacy=await engine.legacy_devices(QueryDevicesRequest(space_id="153",keyword="水 泵",recursive=True,limit=200))
    assert legacy["count"]==97
    assert {"equip_no","equip_name","space_id","space_path","space_link","metadata"} <= legacy["devices"][0].keys()
    old_count=await engine.legacy_collection(QueryScopeCollectionRequest(root_space_id="153",target_entity_level="equipment",target_equipment_type="水泵",output_mode="count"))
    assert old_count["count"]==115 and old_count["returned_count"]==0
    generic=await engine.legacy_collection(QueryScopeCollectionRequest(root_space_id="231",target_entity_level="equipment",target_equipment_type="设备"))
    assert generic["count"]==6


@pytest.mark.asyncio
async def test_governed_parent_alias_type_mapping_and_version_frozen(env):
    engine,db,s,q,insert,owner,path=env
    policy={"schema_version":"1.0","reviewed_by":"fixture-reviewer","categories":[
        {"dimension":"equipment_class","id":"pump","name":"泵","aliases":["泵类"],"tags":[]},
        {"dimension":"equipment_class","id":"equipment.pump.water","name":"水泵","parent":"pump","tags":["equipment.pump.water"],"type_codes":["PUMP"]}]}
    file=path/"policy.json";file.write_text(json.dumps(policy));s.asset_taxonomy_policy_file=str(file)
    first=await q(current({"field":"equipment_class","operator":"is","value":"泵类","include_descendants":True}))
    assert first["count"]==115
    exact=await q(current({"field":"equipment_class","operator":"is","value":"泵","include_descendants":False}))
    assert exact["count"]==0
    policy["categories"][1]["name"]="水泵新分类名称";file.write_text(json.dumps(policy))
    historic=await q(follow(first["query_id"]))
    assert historic["groups"]==[{"name":"水泵","count":115}]
    assert historic["criteria"]["taxonomy_version"]==first["criteria"]["taxonomy_version"]
    fine=await q(follow(first["query_id"],group_by="equipment_subclass"))
    assert fine["count"]==115 and fine["groups"]==[{"name":"待补充分组资料","count":115}]
    assert fine["result_complete"] is False and fine["grouping"]["dimension_missing_count"]==115


@pytest.mark.asyncio
async def test_role_separation_overlapping_groups_and_missing_group_bucket(env):
    engine,db,s,q,insert,*_=env
    await insert("equipment","MULTI","多用途设备",{"space_link":"153/200","classification_complete_dimensions":["equipment_class"]},
                 ["purpose.cooling","purpose.circulation","management.process_equipment"],["冷却","循环","生产设备"])
    result=await q(current(None,"group",group_by="purpose"))
    assert result["count"]==148 and result["grouping_mode"]=="overlapping" and result["group_total"]==149
    assert {r["name"]:r["count"] for r in result["groups"]}["待补充分组资料"]==147
    with pytest.raises(QueryError):
        await q(current({"field":"equipment_class","operator":"is","value":"生产设备"}))


@pytest.mark.asyncio
async def test_ambiguous_dictionary_does_not_guess(env):
    engine,db,s,q,insert,*_=env
    await insert("equipment","DOUBLE","别的类别",{"space_link":"153/200"},["equipment.some_other"],["水泵"])
    with pytest.raises(QueryError) as e:await q(current(water()))
    assert e.value.code=="CATEGORY_AMBIGUOUS"


@pytest.mark.asyncio
async def test_scope_path_boundary_and_direct_only(env):
    engine,db,s,q,insert,*_=env
    await insert("equipment","OUT","范围外",{"space_link":"1530/200","space_id":"200"})
    total=await q(current())
    direct=await q({"scope":{"root_space_id":"153","recursive":False},"operation":"count"})
    assert total["count"]==147 and direct["count"]==0


@pytest.mark.asyncio
async def test_snapshot_owner_capacity_and_expiry_cleanup(env):
    engine,db,s,q,*_=env
    s.asset_query_snapshot_owner_limit=1
    first=await q(current(water()))
    with pytest.raises(QueryError) as e:await q(current())
    assert e.value.code=="QUERY_STORAGE_LIMIT"
    assert (await q(follow(first["query_id"])))['count']==115
    await db.execute("UPDATE public.phm_asset_query_runs SET expires_at=now()-interval '1 second'")
    assert (await q(current()))['count']==147


@pytest.mark.asyncio
async def test_unsupported_acl_never_silently_uses_shared_catalog(env):
    engine,db,s,q,*_=env
    with pytest.raises(QueryError) as e:await q(current(),{"shared_catalog":False})
    assert e.value.code=="QUERY_SCOPE_UNSUPPORTED"


@pytest.mark.asyncio
async def test_native_tree_uses_same_scope_and_fails_on_cycle_or_depth_limit(env):
    engine,db,s,q,*_=env
    await db.execute("DROP TABLE IF EXISTS phm_test_spaces;CREATE TABLE phm_test_spaces(id text,parent text,name text,kind text,code text);INSERT INTO phm_test_spaces VALUES('153',NULL,'总部钢铁','company','Z'),('200','153','泵区','area','P'),('231','153','粗轧区','area','R')")
    s.asset_hierarchy_backend="native_recursive";s.space_table="phm_test_spaces"
    s.space_id_column="id";s.space_parent_id_column="parent";s.space_name_column="name"
    assert (await q(current(water())))['count']==115
    await db.execute("UPDATE phm_test_spaces SET parent='231' WHERE id='153'")
    with pytest.raises(QueryError) as e:await q(current())
    assert e.value.code=="SCOPE_INCOMPLETE"


@pytest.mark.asyncio
async def test_injection_literals_and_unknown_operators_are_never_sql(env):
    engine,db,s,q,*_=env
    value="水泵' OR true; DROP TABLE phm_test_asset_catalog; --"
    assert (await q(current({"field":"equipment_name","operator":"contains","value":value})))['count']==0
    assert (await q(current()))['count']==147
    with pytest.raises(QueryError):
        await q(current({"field":"equipment_name","operator":"raw_sql","value":"1=1"}))
