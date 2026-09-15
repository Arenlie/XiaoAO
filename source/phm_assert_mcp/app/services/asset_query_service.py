from __future__ import annotations

import asyncio
import copy
import logging
import time
import unicodedata
from typing import Any

from app.config import Settings
from app.repositories.equipment_repository import EquipmentRepository
from app.repositories.point_repository import PointRepository
from app.repositories.space_repository import SpaceRepository
from app.repositories.semantic_repository import SemanticRepository
from app.providers.llm import LlmProvider
from app.performance import current_recorder
from app.schemas.space import QuerySpaceTreeRequest, QuerySpaceChildrenRequest
from app.schemas.equipment import QueryDevicesRequest, QueryEquipmentInfoRequest
from app.schemas.point import QueryPointsRequest
from app.schemas.collection import QueryScopeCollectionRequest
from app.asset_query_contract import GENERIC_CLASSES

logger = logging.getLogger(__name__)


class AssetQueryService:
    def __init__(self, settings: Settings, spaces: SpaceRepository, equipment: EquipmentRepository, points: PointRepository, llm: LlmProvider, semantic: SemanticRepository) -> None:
        self.settings = settings
        self.spaces = spaces
        self.equipment = equipment
        self.points = points
        self.llm = llm
        self.semantic = semantic
        self.collection_engine = None
        self._cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}

    def _cache_get(self, key: tuple[Any, ...]) -> dict[str, Any] | None:
        if not self.settings.asset_cache_enabled:
            return None
        item = self._cache.get(key)
        if not item:
            return None
        expires_at, payload = item
        if expires_at <= time.monotonic():
            self._cache.pop(key, None)
            return None
        return copy.deepcopy(payload)

    def _cache_set(self, key: tuple[Any, ...], payload: dict[str, Any]) -> None:
        if not self.settings.asset_cache_enabled:
            return
        # The cache is intentionally short-lived and process-local. It never affects correctness after TTL expiry.
        self._cache[key] = (time.monotonic() + self.settings.asset_cache_ttl_seconds, copy.deepcopy(payload))
        # Prevent unbounded key growth if callers use highly variable keywords.
        if len(self._cache) > 4096:
            now = time.monotonic()
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            if len(self._cache) > 4096:
                for old_key in list(self._cache)[: len(self._cache) - 4096]:
                    self._cache.pop(old_key, None)

    async def query_space_tree(self, request: QuerySpaceTreeRequest, request_id: str = "") -> dict[str, Any]:
        started = time.perf_counter()
        max_depth = min(request.max_depth, self.settings.asset_tree_max_depth)
        key = ("tree", request.root_space_id, max_depth, request.include_devices, request.include_points)
        cached = self._cache_get(key)
        if cached is not None:
            self._log(request_id, "query_space_tree", request.root_space_id, int(cached.get("node_count") or 0), started, "OK", cache_hit=True)
            return cached

        root, nodes, truncated = await self.spaces.get_tree(request.root_space_id, max_depth, self.settings.asset_tree_max_nodes)
        result_nodes = list(nodes)
        if request.include_devices:
            remaining = max(0, self.settings.asset_tree_max_nodes - len(result_nodes))
            if remaining == 0:
                truncated = True
                devices = []
            else:
                devices, dev_truncated = await self.equipment.query(
                    space_id=request.root_space_id, recursive=True, keyword=None, limit=remaining
                )
                truncated = truncated or dev_truncated
            space_links = {(str(n.get("space_link") or "").strip("/") + "/"): n for n in [root, *nodes] if n.get("space_link")}
            space_ids = {str(n.get("space_id")): n for n in [root, *nodes] if n.get("space_id")}
            equipment_depths: dict[str, int] = {}
            appended_devices: list[dict[str, Any]] = []
            for d in devices:
                if len(result_nodes) >= self.settings.asset_tree_max_nodes:
                    truncated = True
                    break
                parent_id = str(d.get("space_id") or request.root_space_id)
                parent_depth = 0
                if parent_id in space_ids:
                    parent_depth = int(space_ids[parent_id].get("depth") or 0)
                link = str(d.get("space_link") or "").strip("/")
                link = link + "/" if link else ""
                best = 0
                for sl, n in space_links.items():
                    if link.startswith(sl) and len(sl) > best:
                        parent_id = str(n.get("space_id") or request.root_space_id)
                        parent_depth = int(n.get("depth") or 0)
                        best = len(sl)
                equip_depth = parent_depth + 1
                eno = str(d.get("equip_no") or "")
                if eno:
                    equipment_depths[eno] = equip_depth
                result_nodes.append({"node_type": "equipment", **d, "parent_space_id": parent_id, "depth": equip_depth})
                appended_devices.append(d)

            if request.include_points:
                # Query only devices actually appended to the bounded response, never devices discarded by the node cap.
                for d in appended_devices:
                    if len(result_nodes) >= self.settings.asset_tree_max_nodes:
                        truncated = True
                        break
                    eno = d.get("equip_no")
                    if not eno:
                        continue
                    _, pts, pt_truncated = await self.points.query(
                        equip_no=eno,
                        point_type=None,
                        keyword=None,
                        limit=max(1, self.settings.asset_tree_max_nodes - len(result_nodes)),
                    )
                    truncated = truncated or pt_truncated
                    for p in pts:
                        if len(result_nodes) >= self.settings.asset_tree_max_nodes:
                            truncated = True
                            break
                        result_nodes.append(
                            {
                                "node_type": "point",
                                **p,
                                "parent_equip_no": eno,
                                "depth": equipment_depths.get(str(eno), 0) + 1,
                            }
                        )

        max_returned = max((int(n.get("depth") or 0) for n in result_nodes), default=0)
        result = {
            "success": True,
            "status": "OK",
            "root": root,
            "nodes": result_nodes,
            "node_count": len(result_nodes),
            "max_depth_returned": max_returned,
            "truncated": truncated,
        }
        self._cache_set(key, result)
        self._log(request_id, "query_space_tree", request.root_space_id, len(result_nodes), started, "OK")
        return result

    async def query_space_children(self, request: QuerySpaceChildrenRequest, request_id: str = "") -> dict[str, Any]:
        started = time.perf_counter()
        key = ("children", request.space_id, request.child_type or "", request.recursive)
        cached = self._cache_get(key)
        if cached is not None:
            self._log(request_id, "query_space_children", request.space_id, int(cached.get("count") or 0), started, "OK", cache_hit=True)
            return cached
        nodes, truncated = await self.spaces.get_children(
            request.space_id, request.child_type, request.recursive, self.settings.asset_tree_max_nodes
        )
        result = {
            "success": True,
            "status": "OK",
            "space_id": request.space_id,
            "children": nodes,
            "count": len(nodes),
            "recursive": request.recursive,
            "truncated": truncated,
        }
        self._cache_set(key, result)
        self._log(request_id, "query_space_children", request.space_id, len(nodes), started, "OK")
        return result

    async def query_devices(self, request: QueryDevicesRequest, request_id: str = "") -> dict[str, Any]:
        if self.collection_engine is not None:
            return await self.collection_engine.legacy_devices(request)
        started = time.perf_counter()
        key = ("devices", request.space_id, request.recursive, request.keyword or "", request.limit)
        cached = self._cache_get(key)
        if cached is not None:
            self._log(request_id, "query_devices", request.space_id, int(cached.get("count") or 0), started, "OK", cache_hit=True)
            return cached
        devices, truncated = await self.equipment.query(
            space_id=request.space_id, recursive=request.recursive, keyword=request.keyword, limit=request.limit
        )
        result = {"success": True, "status": "OK", "devices": devices, "count": len(devices), "truncated": truncated}
        self._cache_set(key, result)
        self._log(request_id, "query_devices", request.space_id, len(devices), started, "OK")
        return result


    @staticmethod
    def _normalize_collection_type(value: Any) -> str:
        text = unicodedata.normalize("NFKC", str(value or "")).casefold()
        return "".join(text.split())

    async def _resolve_collection_semantic_terms(self, terms: list[str], entity_type: str) -> dict[str, Any]:
        capability = await self.semantic.capability()
        if not capability.get("available"):
            return {"available": False, "capability": capability, "resolved": [], "unresolved": terms}
        dictionary = await self.semantic.tag_dictionary(entity_type)
        decisions = await self.llm.resolve_semantic_tags(terms=terms, candidates=dictionary, entity_type=entity_type)
        resolved: list[dict[str, Any]] = []
        unresolved: list[str] = []
        for term in terms:
            item = decisions.get(term) or {"unresolved": True}
            if item.get("unresolved") or not item.get("tag_code"):
                unresolved.append(term)
            else:
                resolved.append({"input_term": term, **item})
        return {
            "available": True,
            "capability": capability,
            "resolved": resolved,
            "unresolved": unresolved,
            "dictionary_size": len(dictionary),
        }

    async def query_scope_collection(
        self, request: QueryScopeCollectionRequest, request_id: str = ""
    ) -> dict[str, Any]:
        """Return a deterministic descendant collection under one real space.

        Reviewed offline semantic tags are the preferred classification
        authority. The runtime model can only map a user term to an existing tag_code;
        PostgreSQL performs the actual membership/count decision.
        """
        if self.collection_engine is not None and request.target_entity_level == "equipment":
            return await self.collection_engine.legacy_collection(request)
        started = time.perf_counter()
        semantic_filters = list(dict.fromkeys(
            str(x).strip() for x in [request.target_equipment_type, *request.semantic_filters]
            if str(x or '').strip() and str(x).strip().casefold() not in GENERIC_CLASSES
        ))
        key = (
            "scope_collection", request.root_space_id, request.target_entity_level,
            request.target_space_type or "", tuple(semantic_filters), request.output_mode,
            request.recursive, request.limit,
        )
        cached = self._cache_get(key)
        if cached is not None:
            self._log(request_id, "query_scope_collection", request.root_space_id, int(cached.get("count") or 0), started, "OK", cache_hit=True)
            return cached

        root = await self.spaces.get_by_id(request.root_space_id)
        if not root:
            from app.errors import AssetError, ErrorCode
            raise AssetError(ErrorCode.SPACE_NOT_FOUND, "空间实体不存在")

        target_level = request.target_entity_level
        logical_limit = min(int(request.limit), self.settings.asset_collection_max_items)
        collection: list[dict[str, Any]] = []
        uncertain: list[dict[str, Any]] = []
        total_candidates: int | None = None
        returned_count = 0
        truncated = False
        query_strategy: dict[str, Any] = {"mode": "single", "message_cn": "单次查询已覆盖本次集合范围。"}
        category_summary: dict[str, Any] = {}
        semantic_resolution: dict[str, Any] = {"available": False, "resolved": [], "unresolved": []}

        if target_level in {"equipment", "space"} and semantic_filters:
            entity_type = "equipment" if target_level == "equipment" else "area"
            semantic_resolution = await self._resolve_collection_semantic_terms(semantic_filters, entity_type)
            if semantic_resolution.get("available"):
                unresolved = list(semantic_resolution.get("unresolved") or [])
                if unresolved:
                    result = {
                        "success": True,
                        "status": "SEMANTIC_FILTER_UNRESOLVED",
                        "message": "部分资产分类条件无法与已审核标签体系安全对应，未执行统计，避免把无法识别误报为 0。",
                        "root": root,
                        "root_space_id": request.root_space_id,
                        "target_entity_level": target_level,
                        "target_space_type": request.target_space_type,
                        "target_equipment_type": request.target_equipment_type,
                        "semantic_filters": request.semantic_filters,
                        "output_mode": request.output_mode,
                        "semantic_resolution": semantic_resolution,
                        "count": None,
                        "returned_count": 0,
                        "collection": [],
                        "uncertain": [], "uncertain_count": 0, "candidate_count": None,
                        "query_strategy": {"mode": "semantic_filter_unresolved", "message_cn": "未执行数据库计数。"},
                        "category_summary": {"classification_basis": "reviewed_semantic_tags", "trusted_review_statuses": semantic_resolution.get("capability", {}).get("trusted_statuses", [])},
                        "truncated": False,
                    }
                    self._cache_set(key, result)
                    self._log(request_id, "query_scope_collection", request.root_space_id, 0, started, result["status"])
                    return result

                tag_codes = [str(x.get("tag_code")) for x in semantic_resolution.get("resolved", []) if x.get("tag_code")]
                if target_level == "equipment":
                    total_candidates = await self.semantic.count_equipment(space_id=request.root_space_id, recursive=request.recursive, tag_codes=tag_codes)
                    if request.output_mode == "list":
                        wanted = min(total_candidates, logical_limit)
                        page_size = min(self.settings.asset_collection_page_size, self.settings.asset_query_max_devices)
                        offsets = list(range(0, wanted, page_size))
                        semaphore = asyncio.Semaphore(self.settings.asset_collection_max_parallel)
                        async def load_semantic_equipment_page(offset: int) -> list[dict[str, Any]]:
                            async with semaphore:
                                return await self.semantic.query_equipment_page(space_id=request.root_space_id, recursive=request.recursive, tag_codes=tag_codes, limit=min(page_size, wanted-offset), offset=offset)
                        pages = await asyncio.gather(*(load_semantic_equipment_page(offset) for offset in offsets)) if offsets else []
                        collection = [{"entity_type": "equipment", **dict(item), "category_match": "MATCH", "category_evidence": "reviewed_semantic_tags"} for page in pages for item in page]
                        truncated = total_candidates > logical_limit
                        query_strategy = {
                            "mode": "semantic_parallel_paged" if len(offsets) > 1 else "semantic_indexed_list",
                            "page_size": page_size, "page_count": len(offsets),
                            "parallelism": min(self.settings.asset_collection_max_parallel, max(1, len(offsets))),
                            "candidate_count": total_candidates, "logical_limit": logical_limit,
                            "message_cn": "超过单次查询上限，已进行多次并行查询，速度较慢。" if len(offsets)>1 else "已按审核语义标签从 PostgreSQL 精确筛选。",
                        }
                    else:
                        query_strategy = {"mode": "semantic_indexed_count", "message_cn": "已按审核语义标签直接在 PostgreSQL 中完成计数，未加载设备明细。"}
                else:
                    total_candidates = await self.semantic.count_spaces(space_id=request.root_space_id, recursive=request.recursive, tag_codes=tag_codes, target_space_type=request.target_space_type)
                    if request.output_mode == "list":
                        wanted = min(total_candidates, logical_limit)
                        page_size = min(self.settings.asset_collection_page_size, self.settings.asset_tree_max_nodes)
                        offsets = list(range(0, wanted, page_size))
                        semaphore = asyncio.Semaphore(self.settings.asset_collection_max_parallel)
                        async def load_semantic_space_page(offset: int) -> list[dict[str, Any]]:
                            async with semaphore:
                                return await self.semantic.query_space_page(space_id=request.root_space_id, recursive=request.recursive, tag_codes=tag_codes, target_space_type=request.target_space_type, limit=min(page_size,wanted-offset), offset=offset)
                        pages = await asyncio.gather(*(load_semantic_space_page(offset) for offset in offsets)) if offsets else []
                        collection = [dict(item) for page in pages for item in page]
                        truncated = total_candidates > logical_limit
                        query_strategy = {"mode": "semantic_parallel_paged" if len(offsets)>1 else "semantic_indexed_list", "page_size":page_size,"page_count":len(offsets),"parallelism":min(self.settings.asset_collection_max_parallel,max(1,len(offsets))),"candidate_count":total_candidates,"logical_limit":logical_limit,"message_cn":"超过单次查询上限，已进行多次并行查询，速度较慢。" if len(offsets)>1 else "已按审核语义标签从 PostgreSQL 精确筛选。"}
                    else:
                        query_strategy = {"mode":"semantic_indexed_count","message_cn":"已按审核语义标签直接在 PostgreSQL 中完成计数，未加载空间明细。"}
                returned_count = len(collection)
                category_summary = {
                    "classification_basis": "reviewed_semantic_tags",
                    "resolved_filters": semantic_resolution.get("resolved", []),
                    "trusted_review_statuses": semantic_resolution.get("capability", {}).get("trusted_statuses", []),
                    "taxonomy_runtime": "database_reviewed_labels",
                }
                result = {
                    "success": True, "status": "OK", "root": root,
                    "root_space_id": request.root_space_id, "target_entity_level": target_level,
                    "target_space_type": request.target_space_type, "target_equipment_type": request.target_equipment_type,
                    "semantic_filters": request.semantic_filters, "output_mode": request.output_mode,
                    "semantic_resolution": semantic_resolution, "recursive": request.recursive,
                    "collection": collection, "count": int(total_candidates or 0), "returned_count": returned_count,
                    "uncertain": [], "uncertain_count": 0, "candidate_count": int(total_candidates or 0),
                    "query_strategy": query_strategy, "category_summary": category_summary, "truncated": truncated,
                }
                self._cache_set(key, result)
                self._log(request_id, "query_scope_collection", request.root_space_id, int(total_candidates or 0), started, "OK")
                return result

        # Backward-compatible path when semantic columns are not installed, or when no
        # semantic filter was requested. Existing 0.4.5 behavior remains available.
        if target_level == "space":
            nodes, truncated = await self.spaces.get_children(request.root_space_id, None, request.recursive, min(request.limit, self.settings.asset_tree_max_nodes))
            requested_type = self._normalize_collection_type(request.target_space_type)
            if requested_type:
                nodes = [node for node in nodes if requested_type in self._normalize_collection_type(node.get("space_type")) or requested_type in self._normalize_collection_type(node.get("space_name"))]
            collection = [{"entity_type":"space","space_id":item.get("space_id"),"space_name":item.get("space_name"),"space_type":item.get("space_type"),"space_no":item.get("space_no"),"space_path":item.get("path") or item.get("space_path"),"space_link":item.get("space_link"),"parent_space_id":item.get("parent_space_id"),"depth":item.get("depth"),"metadata":item.get("metadata") or {}} for item in nodes[:request.limit]]
            total_candidates = len(collection)
            returned_count = len(collection)
            truncated = truncated or len(nodes)>request.limit
            category_summary = {"classification_basis":"space_hierarchy_type"}
        elif target_level == "equipment":
            total_candidates = await self.equipment.count(space_id=request.root_space_id, recursive=request.recursive, keyword=None)
            if request.output_mode == "count" and not semantic_filters:
                collection=[]; returned_count=0
                query_strategy={"mode":"indexed_count","message_cn":"已直接在 PostgreSQL 中完成设备总数统计，未加载设备明细。"}
                category_summary={"classification_basis":"no_category_filter"}
            else:
                wanted=min(total_candidates,logical_limit); page_size=min(self.settings.asset_collection_page_size,self.settings.asset_query_max_devices); offsets=list(range(0,wanted,page_size)); semaphore=asyncio.Semaphore(self.settings.asset_collection_max_parallel)
                async def load_page(offset:int)->list[dict[str,Any]]:
                    async with semaphore:
                        return await self.equipment.query_page(space_id=request.root_space_id,recursive=request.recursive,keyword=None,limit=min(page_size,wanted-offset),offset=offset)
                pages=await asyncio.gather(*(load_page(offset) for offset in offsets)) if offsets else []; devices=[item for page in pages for item in page]; truncated=total_candidates>logical_limit
                query_strategy={"mode":"parallel_paged" if len(offsets)>1 else "single","page_size":page_size,"page_count":len(offsets),"parallelism":min(self.settings.asset_collection_max_parallel,max(1,len(offsets))),"candidate_count":total_candidates,"logical_limit":logical_limit,"message_cn":"超过单次查询上限，已进行多次并行查询，速度较慢。" if len(offsets)>1 else "单次查询已覆盖本次集合范围。"}
                requested_equipment_type=str(request.target_equipment_type or '').strip()
                if requested_equipment_type:
                    labels=[str(item.get('equipment_type') or '').strip() for item in devices]; distinct_labels=list(dict.fromkeys(label for label in labels if label)); decisions=await self.llm.classify_equipment_types(target_type=requested_equipment_type,type_labels=distinct_labels); matched=[]; status_counts={"MATCH":0,"NO_MATCH":0,"UNCERTAIN":0,"MISSING_TYPE":0}
                    for device in devices:
                        label=str(device.get('equipment_type') or '').strip()
                        if not label:
                            status_counts['MISSING_TYPE']+=1; uncertain.append({"entity_type":"equipment",**dict(device),"category_match":"UNCERTAIN","category_evidence":"equipment_type_missing"}); continue
                        status=decisions.get(label,'UNCERTAIN'); status_counts[status]=status_counts.get(status,0)+1; row={"entity_type":"equipment",**dict(device),"category_match":status,"category_evidence":f"equipment_type={label}"}
                        if status=='MATCH': matched.append(row)
                        elif status=='UNCERTAIN': uncertain.append(row)
                    collection=matched; total_candidates=len(matched); returned_count=len(collection)
                    category_summary={"target_equipment_type":requested_equipment_type,"distinct_type_count":len(distinct_labels),"type_decisions":decisions,"status_counts":status_counts,"classification_basis":"legacy_authoritative_equipment_type_then_conservative_llm_semantics","semantic_capability":semantic_resolution.get('capability',{})}
                else:
                    collection=[{"entity_type":"equipment",**dict(item)} for item in devices]; returned_count=len(collection); category_summary={"classification_basis":"no_category_filter"}
        else:
            # Point semantics are intentionally unchanged in r6.6; user chose not to tag points.
            devices, devices_truncated = await self.equipment.query(space_id=request.root_space_id, recursive=request.recursive, keyword=None, limit=self.settings.asset_query_max_devices)
            semaphore=asyncio.Semaphore(self.settings.asset_collection_max_parallel)
            async def load_points(device:dict[str,Any])->tuple[list[dict[str,Any]],bool]:
                equip_no=str(device.get('equip_no') or '')
                if not equip_no:return [],False
                async with semaphore:
                    _,points,point_truncated=await self.points.query(equip_no=equip_no,point_type=None,keyword=None,limit=min(request.limit,self.settings.asset_query_max_points))
                return [{"entity_type":"point",**dict(point),"equip_no":point.get('equip_no') or equip_no,"equip_name":point.get('equip_name') or device.get('equip_name')} for point in points],point_truncated
            batches=await asyncio.gather(*(load_points(device) for device in devices)); all_points=[]; any_point_truncated=False
            for rows,pt in batches: all_points.extend(rows); any_point_truncated=any_point_truncated or pt
            total_candidates=len(all_points); collection=[] if request.output_mode=='count' else all_points[:request.limit]; returned_count=len(collection); truncated=devices_truncated or any_point_truncated or (request.output_mode=='list' and len(all_points)>request.limit); query_strategy={"mode":"single","message_cn":"测点集合仍使用原始测点目录；本版本未对测点生成语义标签。"}; category_summary={"classification_basis":"point_catalog_no_semantic_tags"}

        result={"success":True,"status":"OK","root":root,"root_space_id":request.root_space_id,"target_entity_level":target_level,"target_space_type":request.target_space_type,"target_equipment_type":request.target_equipment_type,"semantic_filters":request.semantic_filters,"output_mode":request.output_mode,"semantic_resolution":semantic_resolution,"recursive":request.recursive,"collection":collection,"count":int(total_candidates if total_candidates is not None else len(collection)),"returned_count":returned_count,"uncertain":uncertain[:100],"uncertain_count":len(uncertain),"candidate_count":total_candidates if total_candidates is not None else len(collection),"query_strategy":query_strategy,"category_summary":category_summary,"truncated":truncated}
        self._cache_set(key,result); self._log(request_id,"query_scope_collection",request.root_space_id,int(result.get('count') or 0),started,"OK"); return result

    async def query_equipment_info(self, request: QueryEquipmentInfoRequest, request_id: str = "") -> dict[str, Any]:
        started = time.perf_counter()
        key = ("equipment_info", request.equip_no.upper())
        cached = self._cache_get(key)
        if cached is not None:
            self._log(request_id, "query_equipment_info", request.equip_no, 1, started, "OK", cache_hit=True)
            return cached
        equipment = await self.equipment.get_by_no(request.equip_no)
        if not equipment:
            from app.errors import AssetError, ErrorCode

            raise AssetError(ErrorCode.EQUIPMENT_NOT_FOUND, "设备不存在")
        semantic = await self.semantic.equipment_semantics_by_no(request.equip_no)
        if semantic:
            equipment = {**equipment, **semantic}
        result = {
            "success": True,
            "status": "OK",
            "equipment": equipment,
            "semantic_source": "reviewed_offline_tags" if semantic else "legacy_asset_catalog",
        }
        self._cache_set(key, result)
        self._log(request_id, "query_equipment_info", request.equip_no, 1, started, "OK")
        return result

    async def query_points(self, request: QueryPointsRequest, request_id: str = "") -> dict[str, Any]:
        started = time.perf_counter()
        key = ("points", request.equip_no.upper(), request.point_type or "", request.keyword or "", request.limit)
        cached = self._cache_get(key)
        if cached is not None:
            self._log(request_id, "query_points", request.equip_no, int(cached.get("count") or 0), started, "OK", cache_hit=True)
            return cached
        equipment, points, truncated = await self.points.query(
            equip_no=request.equip_no, point_type=request.point_type, keyword=request.keyword, limit=request.limit
        )
        result = {
            "success": True,
            "status": "OK",
            "equipment": {"equip_no": equipment.get("equip_no"), "equip_name": equipment.get("equip_name")},
            "points": points,
            "count": len(points),
            "truncated": truncated,
        }
        self._cache_set(key, result)
        self._log(request_id, "query_points", request.equip_no, len(points), started, "OK")
        return result

    @staticmethod
    def _log(request_id: str, tool: str, scope_id: str, count: int, started: float, status: str, cache_hit: bool = False) -> None:
        logger.info(
            "asset_tool_completed",
            extra={
                "fields": {
                    "request_id": request_id,
                    "tool_name": tool,
                    "scope_id": scope_id,
                    "candidate_count": count,
                    "cache_hit": cache_hit,
                    "total_elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                    "status": status,
                }
            },
        )
