#!/usr/bin/env python3
"""Read-only asset classification/scope audit; no models or snapshot writes."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import get_settings
from app.db import DatabaseManager
from app.services.asset_collection_engine import AssetCollectionEngine
from app.services.asset_taxonomy import Taxonomy, compile_predicate


async def collect(args):
    settings=get_settings();db=DatabaseManager(settings);engine=AssetCollectionEngine(db,settings)
    try:
        pool=await db.get_pool()
        async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read",readonly=True):
            taxonomy=await Taxonomy.load(conn,engine.table,settings.asset_taxonomy_policy_file)
            scope={"root_space_id":args.root_space_id,"recursive":not args.direct_only}
            name,scope_sql,params=await engine._scope(conn,scope)
            predicate={"field":"equipment_class","operator":"is","value":args.category} if args.category else None
            normalized=[]
            condition=compile_predicate(predicate,taxonomy,params,normalized=normalized)
            source=f"SELECT c.equip_no, {engine._row_sql()} AS row_data FROM {engine.table} c WHERE c.entity_type='equipment' AND ({scope_sql})"
            rows=await conn.fetch(f"SELECT equip_no,row_data,({condition}) AS matched FROM ({source}) s ORDER BY equip_no",*params)
            catalog=await conn.fetchrow(f"""SELECT count(*) AS rows,count(DISTINCT equip_no) AS distinct_equipment_codes,
                count(*) FILTER(WHERE NULLIF(metadata->>'equip_type','') IS NULL) AS missing_type_codes,
                count(*) FILTER(WHERE NULLIF(to_jsonb(c)->>'semantic_review_status','') IS NULL) AS missing_review_status
                FROM {engine.table} c WHERE entity_type='equipment'""")
            indexes=await conn.fetch("SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=$1 AND tablename=$2",*(engine.table.split('.',1) if '.' in engine.table else ('public',engine.table)))
            report={"collected_at":datetime.now(timezone.utc).isoformat(),"read_only":True,"scope":scope,"scope_name":name,
                    "catalog":dict(catalog),"taxonomy_version":taxonomy.version,"normalized_conditions":normalized,
                    "confirmed_rows":sum(r["matched"] is True for r in rows),"unknown_rows":sum(r["matched"] is None for r in rows),
                    "scoped_rows":len(rows),"scoped_unique_codes":len({r["equip_no"] for r in rows}),
                    "snapshot_schema_present":bool(await conn.fetchval("SELECT to_regclass('public.phm_asset_query_runs')")),
                    "may_create_storage":await conn.fetchval("SELECT has_schema_privilege(current_user,'public','CREATE')"),
                    "indexes":[dict(r) for r in indexes],
                    "classification_dictionary":list(taxonomy.entries.values()),
                    "members":[{"equip_no":r["equip_no"],"name":r["row_data"].get("equipment_name"),
                                "type_code":(r["row_data"].get("metadata") or {}).get("equip_type"),
                                "tag_codes":r["row_data"].get("tag_codes"),"tag_names":r["row_data"].get("tag_names"),
                                "review_status":r["row_data"].get("semantic_review_status"),"matched":r["matched"]} for r in rows],
                    "coverage_note":"此报告验证当前目录和分类资料；上游同步是否完整需与资产同步任务或权威业务表核对，不能仅凭标签数量断言。"}
            args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
            args.output.chmod(0o600)
            print(f"只读核查完成：范围内 {len(rows)} 条，确认符合 {report['confirmed_rows']} 条，待确认 {report['unknown_rows']} 条。")
            print(f"报告：{args.output}")
    finally:
        await db.close()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root-space-id",required=True)
    parser.add_argument("--category",default="")
    parser.add_argument("--direct-only",action="store_true")
    parser.add_argument("--output",type=Path,default=Path("asset_collection_audit.json"))
    arguments=parser.parse_args()
    try:asyncio.run(collect(arguments))
    except Exception as exc:
        print(f"核查未完成：{getattr(exc,'code',type(exc).__name__)}；未输出连接配置或凭据。",file=sys.stderr)
        sys.exit(1)
