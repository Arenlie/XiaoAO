"""One SQL predicate and one immutable collection for asset count/list/group."""
from __future__ import annotations

import base64
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import json
import logging
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from app.asset_query_contract import AssetQuery, QueryError, canonical, normalize_tree, signature, verify_context, validate_predicate
from app.services.asset_taxonomy import Taxonomy, compile_predicate

logger = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS public.phm_asset_query_runs (
 query_id uuid PRIMARY KEY, subject text NOT NULL, conversation_id text NOT NULL,
 branch_id text NOT NULL, task_id text NOT NULL, call_id text NOT NULL,
 request_signature text NOT NULL, criteria_signature text NOT NULL,
 criteria jsonb NOT NULL, taxonomy jsonb NOT NULL, scope_name text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL,
 matched_count bigint NOT NULL, unknown_count bigint NOT NULL,
 scope_count bigint NOT NULL, payload_bytes bigint NOT NULL DEFAULT 0,
 UNIQUE(subject,conversation_id,branch_id,task_id,call_id)
);
CREATE INDEX IF NOT EXISTS phm_asset_query_expiry ON public.phm_asset_query_runs(expires_at);
CREATE INDEX IF NOT EXISTS phm_asset_query_owner ON public.phm_asset_query_runs(subject,conversation_id,branch_id);
CREATE TABLE IF NOT EXISTS public.phm_asset_query_members (
 query_id uuid NOT NULL REFERENCES public.phm_asset_query_runs(query_id) ON DELETE CASCADE,
 member_key text NOT NULL, row_data jsonb NOT NULL, matched boolean,
 PRIMARY KEY(query_id,member_key)
);
CREATE TABLE IF NOT EXISTS public.phm_asset_query_capacity (id int PRIMARY KEY CHECK(id=1), epoch bigint NOT NULL);
INSERT INTO public.phm_asset_query_capacity VALUES(1,0) ON CONFLICT DO NOTHING;
"""

METADATA_FIELDS = ("equip_name", "equip_type", "equipment_type", "space_id", "leaf_space_id", "space_link",
                   "space_path", "area_name", "company_name", "plant_name", "line_name", "leaf_space_name",
                   "equipment_model", "model", "classification_complete_dimensions")


def snapshot_row(row, taxonomy):
    """Freeze only fields used in results/refinement; never waveforms or embeddings."""
    md = row.get("metadata") or {}
    row["metadata"] = {k: md[k] for k in METADATA_FIELDS if k in md}
    groups = {d: taxonomy.group_values(row, d) for d in ("equipment_class", "equipment_subclass", "purpose", "structure_type")}
    for dimension, key in (("company", "company_name"), ("plant", "plant_name"), ("line", "line_name"), ("area", "area_name"), ("model", "equipment_model")):
        value = md.get(key) or (md.get("model") if dimension == "model" else None)
        groups[dimension] = [str(value)] if value else []
    row["groups"] = groups
    return row


class AssetCollectionEngine:
    def __init__(self, db, settings, llm=None):
        self.db, self.settings, self.llm = db, settings, llm
        self.table = settings.asset_catalog_table

    async def _resolve_equipment_class_fallback(self, node, taxonomy):
        """Rewrite only unsupported equipment_class atoms to real reviewed tag ids.

        Strict taxonomy resolution is always attempted first by ``compile_predicate``.
        This fallback runs only after CATEGORY_UNSUPPORTED/CATEGORY_AMBIGUOUS and uses
        an LLM to infer a temporary hierarchy over real reviewed catalog tags.  No
        inferred parent/child relation is persisted.
        """
        if node is None:
            return None, []
        if "not" in node:
            child, traces = await self._resolve_equipment_class_fallback(node["not"], taxonomy)
            return {"not": child}, traces
        for key in ("all", "any"):
            if key in node:
                children, traces = [], []
                for child in node[key]:
                    rewritten, child_traces = await self._resolve_equipment_class_fallback(child, taxonomy)
                    children.append(rewritten)
                    traces.extend(child_traces)
                return {key: children}, traces
        if node.get("field") != "equipment_class":
            return dict(node), []
        try:
            taxonomy.resolve(node)
            return dict(node), []
        except QueryError as exc:
            if exc.code not in {"CATEGORY_UNSUPPORTED", "CATEGORY_AMBIGUOUS"}:
                raise
            strict_error = exc

        term = str(node.get("category_id") or node.get("value") or "").strip()
        candidates, candidate_total = taxonomy.fallback_candidates("equipment_class", term, limit=500)
        if not self.llm or not candidates:
            raise strict_error
        resolution = await self.llm.resolve_equipment_category_fallback(
            input_term=term, candidates=candidates, candidate_total=candidate_total
        )
        relation_selected = {
            str(item.get("tag_code"))
            for item in resolution.get("items") or []
            if isinstance(item, dict)
            and bool(item.get("selected"))
            and str(item.get("relation") or "").upper() in {"SAME", "CHILD"}
        }
        selected = []
        for code in resolution.get("selected_tag_codes") or []:
            code = str(code)
            if code not in relation_selected:
                continue
            key = ("equipment_class", code)
            entry = taxonomy.entries.get(key)
            if entry and entry.get("source") == "reviewed_catalog":
                selected.append(code)
        selected = list(dict.fromkeys(selected))
        if not selected:
            raise strict_error

        atoms = [
            {
                "field": "equipment_class",
                "operator": "is",
                "category_id": code,
                "include_descendants": False,
            }
            for code in selected
        ]
        replacement = atoms[0] if len(atoms) == 1 else {"any": atoms}
        relevant_items = [
            item for item in resolution.get("items") or []
            if isinstance(item, dict) and (item.get("selected") or item.get("relation") in {"SAME", "CHILD", "PARENT", "RELATED"})
        ][:80]
        trace = {
            "field": "equipment_class",
            "input_term": term,
            "strict_error": strict_error.code,
            "resolution_source": "llm_temporary_hierarchy",
            "hierarchy_persisted": False,
            "candidate_source": "reviewed_catalog_tags",
            "candidate_total": int(candidate_total),
            "candidate_sent": len(candidates),
            "candidate_preview": candidates[:40],
            "selected_tag_codes": selected,
            "selected_tags": [
                {"tag_code": code, "tag_name": taxonomy.entries[("equipment_class", code)]["name"]}
                for code in selected
            ],
            "llm_relations": relevant_items,
            "coverage_complete": bool(resolution.get("coverage_complete")),
            "reason": str(resolution.get("reason") or "")[:500],
        }
        logger.info(
            "asset_category_fallback_resolved",
            extra={"fields": {
                "input_term": term,
                "strict_error": strict_error.code,
                "candidate_total": int(candidate_total),
                "candidate_sent": len(candidates),
                "selected_tag_codes": selected,
                "selected_tags": trace["selected_tags"],
                "coverage_complete": trace["coverage_complete"],
                "hierarchy_persisted": False,
            }},
        )
        return replacement, [trace]

    async def _compile_with_category_fallback(self, predicate, taxonomy, base_args):
        args = list(base_args)
        normalized = []
        try:
            return compile_predicate(predicate, taxonomy, args, normalized=normalized), args, normalized, predicate, []
        except QueryError as exc:
            if exc.code not in {"CATEGORY_UNSUPPORTED", "CATEGORY_AMBIGUOUS"}:
                raise
            strict_error = exc
        effective, traces = await self._resolve_equipment_class_fallback(predicate, taxonomy)
        if not traces:
            raise strict_error
        # Revalidate the rewritten boolean tree: it may contain an OR of multiple
        # true tag ids selected from the real reviewed dictionary.
        validate_predicate(effective)
        args = list(base_args)
        normalized = []
        condition = compile_predicate(effective, taxonomy, args, normalized=normalized)
        normalized.extend({"category_resolution": trace} for trace in traces)
        return condition, args, normalized, effective, traces

    async def migrate(self):
        if await self.ready():
            return
        await self.db.execute(DDL)
        if not await self.ready():
            raise QueryError("QUERY_STORAGE_NOT_READY", "资产查询暂存表结构或读写权限不完整")

    async def ready(self):
        expected = {"phm_asset_query_runs": {"query_id","subject","conversation_id","branch_id","task_id","call_id","criteria","taxonomy","request_signature","criteria_signature","created_at","expires_at","payload_bytes","matched_count","unknown_count","scope_count","scope_name"},
                    "phm_asset_query_members": {"query_id","member_key","row_data","matched"},
                    "phm_asset_query_capacity": {"id","epoch"}}
        rows=await self.db.fetch("SELECT table_name,column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=ANY($1::text[])",list(expected))
        actual={key:{r['column_name'] for r in rows if r['table_name']==key} for key in expected}
        if any(not cols <= actual[key] for key,cols in expected.items()):
            return False
        for table,privileges in (("phm_asset_query_runs",("SELECT","INSERT","DELETE")),("phm_asset_query_members",("SELECT","INSERT")),("phm_asset_query_capacity",("SELECT","UPDATE"))):
            for privilege in privileges:
                value=await self.db.fetchrow("SELECT has_table_privilege(current_user,$1,$2) AS ok","public."+table,privilege)
                if not value or not value["ok"]:
                    return False
        return True

    async def query(self, raw_query, request_context):
        if not self.settings.asset_unified_query_enabled:
            raise QueryError("QUERY_DISABLED", "统一资产查询尚未启用")
        owner = verify_context(self.settings.asset_query_context_secret, request_context, raw_query)
        return await self.execute(raw_query, owner)

    async def execute(self, raw_query, owner, *, legacy=False):
        for attempt in range(3):
            try:
                return await self._execute(raw_query, owner, legacy=legacy)
            except Exception as exc:
                if getattr(exc, "sqlstate", None) not in {"40001", "23505", "40P01"} or attempt == 2:
                    raise

    async def _execute(self, raw_query, owner, *, legacy=False):
        try:
            if legacy:
                copy = {**raw_query, "predicate": None}
                query = AssetQuery.model_validate(copy)
                validate_predicate(raw_query.get("predicate"), allow_legacy=True)
                query = query.model_copy(update={"predicate": raw_query.get("predicate")})
            else:
                query = AssetQuery.model_validate(raw_query)
        except (ValidationError, ValueError) as exc:
            if isinstance(exc, QueryError):
                raise
            raise QueryError("INVALID_QUERY", "资产查询条件不完整或互相矛盾") from exc
        request_sig = signature(query.model_dump(mode="json"))
        pool = await self.db.get_pool()
        async with pool.acquire() as conn:
            # A short transaction, ended before the tool result or model answer.
            async with conn.transaction(isolation="repeatable_read"):
                if query.cursor:
                    query_id, offset = self._read_cursor(query.cursor, owner, query)
                    run = await self._get(conn, query_id, owner)
                    return await self._render(conn, run, query, offset=offset, owner=owner)
                if query.reference and query.reference.mode == "same_set":
                    run = await self._get(conn, query.reference.query_id, owner)
                    return await self._render(conn, run, query, owner=owner)
                existing = None if legacy else await conn.fetchrow("""SELECT * FROM public.phm_asset_query_runs WHERE subject=$1
                    AND conversation_id=$2 AND branch_id=$3 AND task_id=$4 AND call_id=$5""", *self._owner(owner), owner["task_id"], owner["call_id"])
                if existing:
                    if existing["request_signature"] != request_sig:
                        raise QueryError("QUERY_RETRY_CONFLICT", "同一次查询重试的条件发生变化，请重新发起查询")
                    retention_deadline = existing["created_at"] + timedelta(seconds=self.settings.asset_query_snapshot_retention_seconds)
                    if retention_deadline <= datetime.now(timezone.utc):
                        raise QueryError("QUERY_EXPIRED", "原查询快照已超过历史保留期，请重新查询当前数据")
                    return await self._render(conn, existing, query, owner=owner)
                parent = None
                if query.reference:
                    parent = await self._get(conn, query.reference.query_id, owner)
                if parent and query.reference.mode == "refine_set":
                    if query.scope:
                        raise QueryError("INVALID_QUERY", "原集合内筛选不能替换查询区域，请发起新的查询")
                    scope = parent["criteria"]["scope"]
                    # Frozen category dictionary as well as frozen rows.
                    taxonomy = Taxonomy([], parent["taxonomy"], frozen_version=parent["criteria"]["taxonomy_version"])
                    scope_name = parent["scope_name"]
                    base_args = [parent["query_id"]]
                    condition, args, normalized, effective_refine, category_resolution = await self._compile_with_category_fallback(
                        query.predicate, taxonomy, base_args
                    )
                    source = f"SELECT member_key,row_data,(matched AND {condition}) AS matched FROM public.phm_asset_query_members WHERE query_id=$1"
                    predicate = {"all": [p for p in [parent["criteria"].get("predicate"), effective_refine] if p]} or None
                    if not predicate["all"]:
                        predicate = None
                    scope_count = parent["scope_count"]
                else:
                    scope = query.scope.model_dump() if query.scope else parent["criteria"]["scope"]
                    predicate = query.predicate
                    if parent and query.reference.mode == "refresh" and predicate is None:
                        predicate = parent["criteria"].get("predicate")
                    taxonomy = await Taxonomy.load(conn, self.table, self.settings.asset_taxonomy_policy_file)
                    taxonomy.normalize_function = self.settings.asset_normalize_function
                    scope_name, scope_sql, base_args = await self._scope(conn, scope)
                    requested_predicate = predicate
                    condition, args, normalized, predicate, category_resolution = await self._compile_with_category_fallback(
                        predicate, taxonomy, base_args
                    )
                    data = self._row_sql(legacy=legacy)
                    # Identical duplicated rows count once. Conflicting records with
                    # the same equipment code abort below, never choose one silently.
                    source = f"""WITH scoped AS (SELECT {data} AS row_data,
                        COALESCE(NULLIF(upper(c.equip_no),''),'entity:'||c.entity_key) AS member_key
                        FROM {self.table} c WHERE c.entity_type='equipment' AND ({scope_sql})),
                        unique_rows AS (SELECT member_key,min(row_data::text)::jsonb AS row_data,
                        count(DISTINCT row_data)::int AS variants FROM scoped GROUP BY member_key)
                        SELECT member_key,row_data,({condition}) AS matched,variants FROM unique_rows"""
                    scope_count = None
                stats = dict(await conn.fetchrow(f"""WITH candidates AS ({source}) SELECT count(*) AS scope_count,
                    count(*) FILTER(WHERE matched IS TRUE) AS matched_count,
                    count(*) FILTER(WHERE matched IS NULL) AS unknown_count,
                    COALESCE(sum(octet_length(row_data::text)) FILTER(WHERE matched IS NOT FALSE),0)::bigint AS payload_bytes,
                    count(*) FILTER(WHERE matched IS NOT FALSE) AS saved_count
                    {',max(variants) AS variants' if not (parent and query.reference.mode == 'refine_set') else ''}
                    FROM candidates""", *args))
                if int(stats.get("variants") or 0) > 1:
                    raise QueryError("CATALOG_CONFLICT", "同一设备编码存在互相矛盾的资产记录，暂时无法给出可靠的统计结果")
                criteria = {"schema_version": "2.0", "scope": scope, "predicate": normalize_tree(predicate),
                            "normalized_conditions": normalized, "taxonomy_version": taxonomy.version,
                            "parent_query_id": str(parent["query_id"]) if parent else None}
                if category_resolution:
                    criteria["category_resolution"] = category_resolution
                    if not (parent and query.reference.mode == "refine_set"):
                        criteria["requested_predicate"] = normalize_tree(requested_predicate)
                if legacy and query.operation == "count":
                    return {"success": True, "status": "PARTIAL" if stats["unknown_count"] else "OK",
                            "count": stats["matched_count"], "unknown_count": stats["unknown_count"],
                            "scope_name": scope_name, "criteria": criteria, "devices": [],
                            "result_complete": stats["unknown_count"] == 0, "snapshot_available": False}
                if stats["saved_count"] > self.settings.asset_query_snapshot_max_members or stats["payload_bytes"] > self.settings.asset_query_snapshot_max_bytes:
                    return self._oversized(stats, criteria, scope_name, query)
                rows = await conn.fetch(f"SELECT member_key,row_data,matched FROM ({source}) candidates WHERE matched IS NOT FALSE ORDER BY member_key", *args)
                values = [(r["member_key"], dict(r["row_data"]) if legacy else snapshot_row(dict(r["row_data"]), taxonomy), r["matched"]) for r in rows]
                if legacy:
                    from app.repositories.equipment_repository import EquipmentRepository
                    from app.repositories.semantic_repository import SemanticRepository
                    devices = [{**EquipmentRepository._to_device(r[1]), **SemanticRepository._equipment_view(r[1])} for r in values if r[2] is True]
                    return {"success": True, "status": "PARTIAL" if stats["unknown_count"] else "OK",
                            "count": stats["matched_count"], "unknown_count": stats["unknown_count"],
                            "scope_name": scope_name, "criteria": criteria,
                            "devices": devices if query.operation != "count" else [],
                            "result_complete": stats["unknown_count"] == 0, "snapshot_available": False}
                size = sum(len(canonical(r[1]).encode()) for r in values)
                if size > self.settings.asset_query_snapshot_max_bytes:
                    return self._oversized(stats, criteria, scope_name, query)
                # Capacity lock only for bounded persistence; reads don't acquire it.
                # Concurrent creators may receive serialization failure and retry.
                await conn.execute("SELECT pg_advisory_xact_lock(7315050001)")
                # A concurrent writer invalidates the earlier RR read snapshot and
                # forces a retry, so capacity accounting cannot use stale totals.
                await conn.execute("UPDATE public.phm_asset_query_capacity SET epoch=epoch+1 WHERE id=1")
                await conn.execute(
                    "DELETE FROM public.phm_asset_query_runs WHERE created_at <= now() - $1::int * interval '1 second'",
                    self.settings.asset_query_snapshot_retention_seconds,
                )
                quota = await conn.fetchrow("""SELECT COALESCE(sum(payload_bytes),0) AS bytes,
                    count(*) FILTER(WHERE subject=$1 AND conversation_id=$2) AS own
                    FROM public.phm_asset_query_runs""", owner["subject"], owner["conversation_id"])
                if quota["own"] >= self.settings.asset_query_snapshot_owner_limit or quota["bytes"] + size > self.settings.asset_query_snapshot_global_bytes:
                    raise QueryError("QUERY_STORAGE_LIMIT", "查询结果暂存空间已满，请稍后重试；已有查询结果仍可使用")
                query_id = uuid4()
                frozen_taxonomy = {**taxonomy.policy, "categories": [e for e in taxonomy.entries.values() if e["dimension"] != "legacy_tag"]}
                run = await conn.fetchrow("""INSERT INTO public.phm_asset_query_runs
                    (query_id,subject,conversation_id,branch_id,task_id,call_id,request_signature,criteria_signature,
                     criteria,taxonomy,scope_name,expires_at,matched_count,unknown_count,scope_count,payload_bytes)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,now()+$12::int*interval '1 second',$13,$14,$15,$16) RETURNING *""",
                    query_id, *self._owner(owner), owner["task_id"], owner["call_id"], request_sig,
                    signature({"scope": scope, "predicate": criteria["predicate"], "taxonomy": taxonomy.version}),
                    criteria, frozen_taxonomy, scope_name, self.settings.asset_query_snapshot_ttl_seconds,
                    stats["matched_count"], stats["unknown_count"], scope_count if scope_count is not None else stats["scope_count"], size)
                if values:
                    await conn.executemany("INSERT INTO public.phm_asset_query_members(query_id,member_key,row_data,matched) VALUES($1,$2,$3,$4)", [(query_id, *r) for r in values])
                return await self._render(conn, run, query, owner=owner)

    @staticmethod
    def _owner(owner):
        return owner["subject"], owner["conversation_id"], owner["branch_id"]

    async def legacy_devices(self, request):
        result = await self.execute({"scope": {"root_space_id": request.space_id, "recursive": request.recursive},
            "predicate": {"field": "legacy_search_text", "operator": "contains", "value": request.keyword} if request.keyword else None,
            "operation": "list"}, {}, legacy=True)
        devices = result.get("devices", [])[:request.limit]
        return {"success": True, "status": result["status"], "devices": devices, "count": len(devices),
                "total_count": result["count"], "unknown_count": result["unknown_count"],
                "truncated": len(devices) < result["count"], "query_semantics": "legacy_search_text"}

    async def legacy_collection(self, request):
        terms = list(dict.fromkeys(str(t).strip() for t in [request.target_equipment_type, *request.semantic_filters] if str(t or '').strip()))
        clauses = [{"field": "legacy_tag", "operator": "is", "value": t} for t in terms]
        result = await self.execute({"scope": {"root_space_id": request.root_space_id, "recursive": request.recursive},
            "predicate": {"all": clauses} if clauses else None, "operation": request.output_mode}, {}, legacy=True)
        devices = result.get("devices", [])[:request.limit]
        return {"success": True, "status": result["status"], "root_space_id": request.root_space_id,
                "root": {"space_id": request.root_space_id, "space_name": result["scope_name"]},
                "target_entity_level": "equipment", "target_equipment_type": request.target_equipment_type,
                "recursive": request.recursive, "output_mode": request.output_mode,
                "collection": devices, "count": result["count"], "returned_count": len(devices),
                "uncertain_count": result["unknown_count"], "unknown_count": result["unknown_count"],
                "truncated": request.output_mode == "list" and len(devices) < result["count"],
                "query_strategy": {"mode": "unified_sql", "message_cn": "已按统一条件查询设备目录。"},
                "category_summary": {"classification_basis": "reviewed_semantic_tags" if terms else "no_category_filter"}}

    async def _get(self, conn, query_id, owner):
        try:
            key = UUID(str(query_id))
        except ValueError:
            raise QueryError("QUERY_NOT_FOUND", "找不到可供当前对话使用的原查询结果") from None
        inherited = str(key) in owner.get("allowed_query_ids", [])
        row = await conn.fetchrow("SELECT * FROM public.phm_asset_query_runs WHERE query_id=$1 AND subject=$2 AND conversation_id=$3 AND (branch_id=$4 OR $5::boolean)", key, *self._owner(owner), inherited)
        if not row:
            raise QueryError("QUERY_NOT_FOUND", "找不到可供当前对话使用的原查询结果")
        retention_deadline = row["created_at"] + timedelta(seconds=self.settings.asset_query_snapshot_retention_seconds)
        if retention_deadline <= datetime.now(timezone.utc):
            raise QueryError("QUERY_EXPIRED", "原查询快照已超过历史保留期，无法继续按原集合分析；可以重新查询当前数据")
        return row

    async def _scope(self, conn, scope):
        if scope.get("scope_type") == "shared_catalog":
            # The request reached this point only after verify_context accepted a
            # trusted Conversation signature with shared_catalog=true.  Query the
            # complete equipment catalog instead of inventing a root asset entity.
            return "全部资产", "TRUE", []
        s, root = self.settings, scope["root_space_id"]
        if s.asset_hierarchy_backend == "catalog_path":
            rows = await conn.fetch(f"""SELECT DISTINCT COALESCE(metadata->>'leaf_space_name',display_name) AS name,
                trim(both '/' from metadata->>'space_link') AS link FROM {self.table}
                WHERE entity_type='area' AND (metadata->>'space_id'=$1 OR entity_key=$1)""", root)
            if len(rows) != 1:
                raise QueryError("SCOPE_NOT_RESOLVED", "查询区域不存在或存在互相矛盾的范围记录")
            if scope["recursive"]:
                if not rows[0]["link"]:
                    raise QueryError("SCOPE_INCOMPLETE", "区域层级资料不完整，暂时无法确定全部下级设备")
                args = [rows[0]["link"] + "/"]
                sql = "left(trim(both '/' from COALESCE(c.metadata->>'space_link',''))||'/',length($1))=$1"
            else:
                args, sql = [root], "(c.metadata->>'space_id'=$1 OR c.metadata->>'leaf_space_id'=$1)"
            return rows[0]["name"] or root, sql, args
        root_row = await conn.fetchrow(f"SELECT {s.space_name_column}::text AS name FROM {s.space_table} WHERE {s.space_id_column}::text=$1", root)
        if not root_row:
            raise QueryError("SCOPE_NOT_RESOLVED", "查询区域不存在")
        ids = [root]
        if scope["recursive"]:
            rows = await conn.fetch(f"""WITH RECURSIVE tree AS (
                SELECT {s.space_id_column}::text AS id,ARRAY[{s.space_id_column}::text] AS seen,0 AS depth,false AS cycle
                FROM {s.space_table} WHERE {s.space_id_column}::text=$1
                UNION ALL SELECT n.{s.space_id_column}::text,t.seen||n.{s.space_id_column}::text,t.depth+1,n.{s.space_id_column}::text=ANY(t.seen)
                FROM {s.space_table} n JOIN tree t ON n.{s.space_parent_id_column}::text=t.id
                WHERE NOT t.cycle AND t.depth<=$2)
                SELECT * FROM tree LIMIT $3""", root, s.asset_tree_max_depth, s.asset_tree_max_nodes+1)
            if len(rows) > s.asset_tree_max_nodes or any(r["cycle"] or r["depth"] > s.asset_tree_max_depth for r in rows):
                raise QueryError("SCOPE_INCOMPLETE", "区域层级超出查询范围或存在循环，未将部分数据作为全部结果")
            ids = [r["id"] for r in rows]
        return root_row["name"] or root, "(c.metadata->>'space_id'=ANY($1::text[]) OR c.metadata->>'leaf_space_id'=ANY($1::text[]))", [ids]

    @staticmethod
    def _row_sql(*, legacy=False):
        md = ",".join(f"'{k}',c.metadata->'{k}'" for k in METADATA_FIELDS)
        metadata_sql = "c.metadata" if legacy else f"jsonb_strip_nulls(jsonb_build_object({md}))"
        extras = ",".join(f"'{k}',to_jsonb(c)->'{k}'" for k in ("normalized_name", "search_aliases", "semantic_keywords", "semantic_confidence", "taxonomy_version", "semantic_profile_summary"))
        return f"""jsonb_build_object('equip_no',c.equip_no,'entity_key',c.entity_key,
            'display_name',c.display_name,
            'equipment_name',COALESCE(NULLIF(c.metadata->>'equip_name',''),NULLIF(c.display_name,'')),
            'legacy_search_text',concat_ws(' ',c.display_name,c.metadata->>'equip_name',c.metadata->>'equipment_type',c.metadata->>'equip_type',c.search_text),
            'metadata',{metadata_sql},
            'tag_codes',COALESCE(NULLIF(to_jsonb(c)->'tag_codes','null'::jsonb),'[]'::jsonb),
            'tag_names',COALESCE(NULLIF(to_jsonb(c)->'tag_names','null'::jsonb),'[]'::jsonb),
            'semantic_review_status',to_jsonb(c)->>'semantic_review_status')""" + (f"||jsonb_build_object({extras})" if legacy else "")

    async def _render(self, conn, run, query, offset=0, owner=None):
        count, unknown = int(run["matched_count"]), int(run["unknown_count"])
        result = {"success": True, "status": "PARTIAL" if unknown else "OK", "schema_version": "2.0",
            "request_signature": signature(query.model_dump(mode="json")),
            "query_id": str(run["query_id"]), "criteria_signature": run["criteria_signature"],
            "criteria": run["criteria"], "scope_name": run["scope_name"], "operation": query.operation,
            "group_by": query.group_by, "snapshot_at": run["created_at"].isoformat(),
            "expires_at": run["expires_at"].isoformat(), "freshness": query.freshness,
            "count": count, "matched_count": count, "unknown_count": unknown, "scope_count": int(run["scope_count"]),
            "result_complete": unknown == 0, "snapshot_available": True, "page_complete": True,
            "source_coverage": {"source": "asset_catalog", "upstream_complete": None},
            "category_resolution": (run["criteria"] or {}).get("category_resolution") or [],
            "devices": [], "groups": [], "next_cursor": None,
            "warnings": [f"另有 {unknown} 台设备的分类或名称资料不足，暂时无法确认是否符合条件；未将其算作不符合。"] if unknown else []}
        if any(not bool(item.get("coverage_complete")) for item in result["category_resolution"] if isinstance(item, dict)):
            result["result_complete"] = False
            result["status"] = "PARTIAL"
            result["warnings"].append("原分类词通过真实审核标签进行了语义纠偏，但模型无法确认所选标签已覆盖该类别的全部语义范围。")
        if query.operation == "list":
            records = await conn.fetch("""SELECT row_data FROM public.phm_asset_query_members WHERE query_id=$1 AND matched IS TRUE
                ORDER BY member_key LIMIT $2 OFFSET $3""", run["query_id"], query.page_size, offset)
            result["devices"] = [self._device(r["row_data"]) for r in records]
            if offset + len(records) < count:
                result["next_cursor"] = self._cursor(run, query, offset+len(records), owner=owner)
                result["page_complete"] = False
            result["returned_count"] = len(records)
        elif query.operation == "group":
            rows = await conn.fetch("""WITH members AS (SELECT member_key,COALESCE(row_data->'groups'->$2,'[]'::jsonb) AS groups
                FROM public.phm_asset_query_members WHERE query_id=$1 AND matched IS TRUE), expanded AS (
                SELECT member_key,g FROM members CROSS JOIN LATERAL jsonb_array_elements_text(
                CASE WHEN jsonb_array_length(groups)=0 THEN '["待补充分组资料"]'::jsonb ELSE groups END) g)
                SELECT g AS name,count(DISTINCT member_key)::int AS count FROM expanded GROUP BY g ORDER BY count DESC,g""", run["query_id"], query.group_by)
            result["groups"] = [dict(r) for r in rows]
            overlap = sum(r["count"] for r in rows) != count
            result["grouping_mode"] = "overlapping" if overlap else "exclusive"
            result["group_total"] = sum(r["count"] for r in rows)
            missing = next((r["count"] for r in rows if r["name"] == "待补充分组资料"), 0)
            result["grouping"] = {"exclusive": not overlap, "unique_member_count": count,
                                  "dimension_missing_count": missing, "dimension_known_count": count-missing}
            if missing:
                result["result_complete"], result["status"] = False, "PARTIAL"
                result["warnings"].append(f"已确认的设备中有 {missing} 台缺少该维度的分组资料，已保留在待补充分组中，未从设备总数中删除。")
            if overlap:
                result["warnings"].append("一台设备可能归入多个类别，各类别数量不能直接相加作为设备总数。")
        return result

    @staticmethod
    def _device(row):
        md = row.get("metadata") or {}
        return {"equip_no": row.get("equip_no"), "equip_name": row.get("equipment_name"),
                "path": md.get("space_path"), "equipment_classes": row.get("groups", {}).get("equipment_class", []),
                "equipment_type_code": md.get("equip_type")}

    def _cursor(self, run, query, offset, owner=None):
        owner = owner or run
        body = {"query_id": str(run["query_id"]), "offset": offset,
                "subject": owner["subject"], "conversation_id": owner["conversation_id"], "branch_id": owner["branch_id"],
                "operation": query.operation, "group_by": query.group_by, "page_size": query.page_size,
                "request": signature(query.model_dump(mode="json", exclude={"cursor"}))}
        raw = base64.urlsafe_b64encode(canonical(body).encode()).decode().rstrip("=")
        return raw + "." + hmac.new(self.settings.asset_query_context_secret.encode(), raw.encode(), hashlib.sha256).hexdigest()

    def _read_cursor(self, token, owner, query):
        try:
            raw, mac = token.rsplit(".", 1)
            expected = hmac.new(self.settings.asset_query_context_secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(mac, expected):
                raise ValueError()
            data = json.loads(base64.urlsafe_b64decode(raw+"="*(-len(raw)%4)))
            if self._owner(data) != self._owner(owner) or data["request"] != signature(query.model_dump(mode="json", exclude={"cursor"})):
                raise ValueError()
            if not isinstance(data["offset"], int) or data["offset"] < 0:
                raise ValueError()
            return data["query_id"], data["offset"]
        except (ValueError, KeyError, TypeError):
            raise QueryError("CURSOR_INVALID", "分页位置已失效或与本次查询条件不一致") from None

    @staticmethod
    def _oversized(stats, criteria, scope_name, query):
        unknown = int(stats["unknown_count"])
        category_resolution = criteria.get("category_resolution") or []
        return {"success": True, "status": "PARTIAL", "schema_version": "2.0", "query_id": None,
                "request_signature": signature(query.model_dump(mode="json")),
                "criteria": criteria, "scope_name": scope_name, "operation": query.operation, "group_by": query.group_by,
                "count": int(stats["matched_count"]), "matched_count": int(stats["matched_count"]),
                "unknown_count": unknown, "scope_count": int(stats["scope_count"]),
                "result_complete": query.operation == "count" and unknown == 0 and all(bool(item.get("coverage_complete")) for item in category_resolution if isinstance(item, dict)),
                "snapshot_available": False, "page_complete": False, "devices": [], "groups": [],
                "category_resolution": category_resolution,
                "warnings": ["已统计当前符合条件的设备数量；结果超过暂存上限，未保存可供后续追问的完整集合。请缩小区域后获取清单或分类。"]
                + (["原分类词通过真实审核标签进行了语义纠偏，但模型无法确认所选标签已覆盖该类别的全部语义范围。"]
                   if any(not bool(item.get("coverage_complete")) for item in category_resolution if isinstance(item, dict)) else [])}
