from __future__ import annotations

from typing import Any

from app.config import Settings
from app.db import DatabaseManager
from app.errors import AssetError, ErrorCode
from app.repositories.space_repository import SpaceRepository


class EquipmentRepository:
    def __init__(self, db: DatabaseManager, settings: Settings, spaces: SpaceRepository) -> None:
        self.db = db
        self.settings = settings
        self.spaces = spaces
        self.table = settings.asset_catalog_table
        self.norm = settings.asset_normalize_function

    async def get_by_no(self, equip_no: str) -> dict[str, Any] | None:
        sql = f"""
            SELECT entity_key, equip_no, display_name, metadata
            FROM {self.table}
            WHERE entity_type='equipment' AND upper(equip_no)=upper($1)
            ORDER BY entity_key
            LIMIT 2
        """
        rows = await self.db.fetch(sql, equip_no)
        if not rows:
            return None
        if len(rows) > 1 and str(rows[0].get("entity_key")) != str(rows[1].get("entity_key")):
            raise AssetError(ErrorCode.NEEDS_DISAMBIGUATION, "设备编码命中多个资产实体，无法安全查询测点")
        return self._to_device(rows[0])

    async def _scope(self, space_id: str, recursive: bool) -> tuple[str, list[str]]:
        space = await self.spaces.get_by_id(space_id)
        if not space:
            raise AssetError(ErrorCode.SPACE_NOT_FOUND, "空间实体不存在")
        link = str(space.get("space_link") or "").strip("/")
        scope_ids = [space_id]
        if self.settings.asset_hierarchy_backend == "catalog_path":
            if not link:
                raise AssetError(ErrorCode.INTERNAL_ERROR, "空间目录缺少 space_link，无法查询下属设备")
            return link + "/", scope_ids
        if recursive:
            _, descendants, _ = await self.spaces.get_tree(
                space_id, self.settings.asset_tree_max_depth, self.settings.asset_tree_max_nodes
            )
            scope_ids.extend(str(n.get("space_id")) for n in descendants if n.get("space_id"))
        return "", list(dict.fromkeys(scope_ids))

    async def count(self, *, space_id: str, recursive: bool, keyword: str | None = None) -> int:
        link, scope_ids = await self._scope(space_id, recursive)
        if self.settings.asset_hierarchy_backend == "catalog_path":
            sql = f"""
                SELECT count(*) AS n FROM {self.table} idx
                WHERE entity_type='equipment'
                  AND (($4::boolean AND left(trim(both '/' from COALESCE(metadata->>'space_link','')) || '/',length($1))=$1)
                       OR (NOT $4::boolean AND (metadata->>'space_id'=$2 OR metadata->>'leaf_space_id'=$2)))
                  AND ($3='' OR {self.norm}(concat_ws(' ',display_name,metadata->>'equip_name',metadata->>'equipment_type',metadata->>'equip_type',search_text)) LIKE '%'||{self.norm}($3)||'%')
            """
            rows = await self.db.fetch(sql, link, space_id, keyword or "", recursive)
            row = rows[0] if rows else {}
        else:
            sql = f"""
                SELECT count(*) AS n FROM {self.table}
                WHERE entity_type='equipment'
                  AND (metadata->>'space_id'=ANY($1::text[]) OR metadata->>'leaf_space_id'=ANY($1::text[]))
                  AND ($2='' OR {self.norm}(concat_ws(' ',display_name,metadata->>'equip_name',metadata->>'equipment_type',metadata->>'equip_type',search_text)) LIKE '%'||{self.norm}($2)||'%')
            """
            rows = await self.db.fetch(sql, scope_ids, keyword or "")
            row = rows[0] if rows else {}
        return int((row or {}).get("n") or 0)

    async def query_page(self, *, space_id: str, recursive: bool, keyword: str | None, limit: int, offset: int = 0) -> list[dict[str, Any]]:
        link, scope_ids = await self._scope(space_id, recursive)
        page_limit = max(1, min(int(limit), self.settings.asset_query_max_devices))
        page_offset = max(0, int(offset))
        if self.settings.asset_hierarchy_backend == "catalog_path":
            sql = f"""
                SELECT entity_key, equip_no, display_name, metadata
                FROM {self.table} idx
                WHERE entity_type='equipment'
                  AND (($6::boolean AND left(trim(both '/' from COALESCE(metadata->>'space_link','')) || '/',length($1))=$1)
                       OR (NOT $6::boolean AND (metadata->>'space_id'=$2 OR metadata->>'leaf_space_id'=$2)))
                  AND ($3='' OR {self.norm}(concat_ws(' ',display_name,metadata->>'equip_name',metadata->>'equipment_type',metadata->>'equip_type',search_text)) LIKE '%'||{self.norm}($3)||'%')
                ORDER BY metadata->>'space_path', COALESCE(metadata->>'equip_name',display_name), equip_no, entity_key
                LIMIT $4 OFFSET $5
            """
            rows = await self.db.fetch(sql, link, space_id, keyword or "", page_limit, page_offset, recursive)
        else:
            sql = f"""
                SELECT entity_key, equip_no, display_name, metadata
                FROM {self.table}
                WHERE entity_type='equipment'
                  AND (metadata->>'space_id'=ANY($1::text[]) OR metadata->>'leaf_space_id'=ANY($1::text[]))
                  AND ($2='' OR {self.norm}(concat_ws(' ',display_name,metadata->>'equip_name',metadata->>'equipment_type',metadata->>'equip_type',search_text)) LIKE '%'||{self.norm}($2)||'%')
                ORDER BY metadata->>'space_path', COALESCE(metadata->>'equip_name',display_name), equip_no, entity_key
                LIMIT $3 OFFSET $4
            """
            rows = await self.db.fetch(sql, scope_ids, keyword or "", page_limit, page_offset)
        return [self._to_device(r) for r in rows]

    async def query(self, *, space_id: str, recursive: bool, keyword: str | None, limit: int) -> tuple[list[dict[str, Any]], bool]:
        effective_limit = min(int(limit), self.settings.asset_query_max_devices)
        rows, total = await __import__("asyncio").gather(
            self.query_page(space_id=space_id, recursive=recursive, keyword=keyword, limit=effective_limit, offset=0),
            self.count(space_id=space_id, recursive=recursive, keyword=keyword),
        )
        return rows, int(total) > effective_limit

    @staticmethod
    def _to_device(row: dict[str, Any]) -> dict[str, Any]:
        m = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        return {
            "equip_id": str(m.get("equip_id") or row.get("entity_key") or "") or None,
            "equip_no": str(row.get("equip_no") or m.get("equip_no") or "") or None,
            "equip_name": str(m.get("equip_name") or row.get("display_name") or "") or None,
            "equipment_type": str(m.get("equipment_type") or m.get("equip_type") or "") or None,
            "space_id": str(m.get("space_id") or m.get("leaf_space_id") or "") or None,
            "space_name": str(m.get("leaf_space_name") or m.get("area_name") or m.get("line_name") or "") or None,
            "space_path": str(m.get("space_path") or "") or None,
            "space_link": str(m.get("space_link") or "") or None,
            "metadata": m,
        }
