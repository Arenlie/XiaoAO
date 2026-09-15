from __future__ import annotations

from typing import Any

from app.config import Settings
from app.db import DatabaseManager
from app.errors import AssetError, ErrorCode


def _md(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("metadata")
    return value if isinstance(value, dict) else {}


def _space_from_catalog(row: dict[str, Any]) -> dict[str, Any]:
    m = _md(row)
    return {
        "node_type": "space",
        "space_id": str(m.get("space_id") or row.get("entity_key") or ""),
        "space_name": str(m.get("leaf_space_name") or row.get("display_name") or m.get("area_name") or ""),
        "space_type": str(m.get("leaf_space_type") or m.get("space_type") or "") or None,
        "space_no": str(m.get("space_number") or m.get("space_code") or "") or None,
        "space_link": str(m.get("space_link") or "") or None,
        "path": str(m.get("space_path") or "") or None,
        "metadata": m,
    }


def _segments(link: str | None) -> list[str]:
    return [p for p in str(link or "").strip("/").split("/") if p]


class SpaceRepository:
    def __init__(self, db: DatabaseManager, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.table = settings.asset_catalog_table

    async def get_by_id(self, space_id: str) -> dict[str, Any] | None:
        if self.settings.asset_hierarchy_backend == "native_recursive":
            return await self._native_get_by_id(space_id)
        sql = f"""
            SELECT entity_key, display_name, metadata
            FROM {self.table}
            WHERE entity_type='area'
              AND (metadata->>'space_id'=$1 OR entity_key=$1)
            ORDER BY CASE WHEN metadata->>'space_id'=$1 THEN 0 ELSE 1 END, entity_key
            LIMIT 2
        """
        rows = await self.db.fetch(sql, space_id)
        if not rows:
            return None
        return _space_from_catalog(rows[0])

    async def get_tree(self, root_space_id: str, max_depth: int, max_nodes: int) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
        if self.settings.asset_hierarchy_backend == "native_recursive":
            return await self._native_tree(root_space_id, max_depth, max_nodes)
        root = await self.get_by_id(root_space_id)
        if not root:
            raise AssetError(ErrorCode.SPACE_NOT_FOUND, "空间实体不存在")
        root_link = str(root.get("space_link") or "").strip("/")
        if not root_link:
            raise AssetError(ErrorCode.INTERNAL_ERROR, "当前目录记录缺少 space_link，无法确定性展开空间树")
        root_link += "/"
        root_segments = _segments(root_link)
        sql = f"""
            WITH dedup AS (
                SELECT DISTINCT ON (COALESCE(NULLIF(metadata->>'space_id',''), entity_key))
                       entity_key, display_name, metadata
                FROM {self.table}
                WHERE entity_type='area'
                  AND left(trim(both '/' from COALESCE(metadata->>'space_link','')) || '/', length($1))=$1
                  AND trim(both '/' from COALESCE(metadata->>'space_link','')) || '/' <> $1
                ORDER BY COALESCE(NULLIF(metadata->>'space_id',''), entity_key), entity_key
            )
            SELECT entity_key, display_name, metadata
            FROM dedup
            ORDER BY length(COALESCE(metadata->>'space_link','')), metadata->>'space_link', entity_key
            LIMIT $2
        """
        rows = await self.db.fetch(sql, root_link, max_nodes + 1)
        truncated = len(rows) > max_nodes
        rows = rows[:max_nodes]
        nodes: list[dict[str, Any]] = []
        allowed_links = {root_link: root_space_id}
        for row in rows:
            node = _space_from_catalog(row)
            link = str(node.get("space_link") or "").strip("/")
            link = link + "/" if link else ""
            depth = max(0, len(_segments(link)) - len(root_segments))
            if depth < 1 or depth > max_depth:
                if depth > max_depth:
                    truncated = True
                continue
            parent_id = root_space_id
            parent_len = len(root_link)
            for ancestor_link, ancestor_id in allowed_links.items():
                if ancestor_link != link and link.startswith(ancestor_link) and len(ancestor_link) > parent_len:
                    parent_id, parent_len = ancestor_id, len(ancestor_link)
            node["parent_space_id"] = str(parent_id)
            node["depth"] = depth
            nodes.append(node)
            allowed_links[link] = node["space_id"]
        return root, nodes, truncated

    async def get_children(self, space_id: str, child_type: str | None, recursive: bool, max_nodes: int) -> tuple[list[dict[str, Any]], bool]:
        if recursive:
            _, nodes, truncated = await self.get_tree(space_id, self.settings.asset_tree_max_depth, max_nodes)
            if child_type:
                normalized = child_type.strip().lower()
                nodes = [n for n in nodes if str(n.get("space_type") or "").lower() == normalized]
            return nodes, truncated
        if self.settings.asset_hierarchy_backend == "native_recursive":
            return await self._native_children(space_id, child_type, max_nodes)

        root = await self.get_by_id(space_id)
        if not root:
            raise AssetError(ErrorCode.SPACE_NOT_FOUND, "空间实体不存在")
        root_link = str(root.get("space_link") or "").strip("/")
        if not root_link:
            raise AssetError(ErrorCode.INTERNAL_ERROR, "当前目录记录缺少 space_link，无法确定性查询下级")
        root_link += "/"
        expected_segments = len(_segments(root_link)) + 1
        normalized_type = (child_type or "").strip().lower()
        sql = f"""
            WITH dedup AS (
                SELECT DISTINCT ON (COALESCE(NULLIF(metadata->>'space_id',''), entity_key))
                       entity_key, display_name, metadata
                FROM {self.table}
                WHERE entity_type='area'
                  AND left(trim(both '/' from COALESCE(metadata->>'space_link','')) || '/', length($1))=$1
                  AND trim(both '/' from COALESCE(metadata->>'space_link','')) || '/' <> $1
                  AND cardinality(regexp_split_to_array(trim(both '/' from COALESCE(metadata->>'space_link','')), '/'))=$2
                  AND ($3='' OR lower(COALESCE(metadata->>'leaf_space_type',metadata->>'space_type',''))=$3)
                ORDER BY COALESCE(NULLIF(metadata->>'space_id',''), entity_key), entity_key
            )
            SELECT entity_key, display_name, metadata
            FROM dedup
            ORDER BY metadata->>'space_link', entity_key
            LIMIT $4
        """
        rows = await self.db.fetch(sql, root_link, expected_segments, normalized_type, max_nodes + 1)
        truncated = len(rows) > max_nodes
        nodes = []
        for row in rows[:max_nodes]:
            node = _space_from_catalog(row)
            node["parent_space_id"] = str(space_id)
            node["depth"] = 1
            nodes.append(node)
        return nodes, truncated

    async def _native_get_by_id(self, space_id: str) -> dict[str, Any] | None:
        s = self.settings
        sql = f"""
            SELECT {s.space_id_column}::text AS space_id,
                   {s.space_parent_id_column}::text AS parent_space_id,
                   {s.space_name_column}::text AS space_name,
                   {s.space_type_column}::text AS space_type,
                   {s.space_no_column}::text AS space_no
            FROM {s.space_table}
            WHERE {s.space_id_column}::text=$1
            LIMIT 1
        """
        row = await self.db.fetchrow(sql, space_id)
        if not row:
            return None
        return {"node_type": "space", **row, "space_link": None, "path": row["space_name"], "metadata": {}}

    async def _native_children(self, space_id: str, child_type: str | None, max_nodes: int) -> tuple[list[dict[str, Any]], bool]:
        s = self.settings
        root = await self._native_get_by_id(space_id)
        if not root:
            raise AssetError(ErrorCode.SPACE_NOT_FOUND, "空间实体不存在")
        normalized_type = (child_type or "").strip().lower()
        sql = f"""
            SELECT {s.space_id_column}::text AS space_id,
                   {s.space_parent_id_column}::text AS parent_space_id,
                   {s.space_name_column}::text AS space_name,
                   {s.space_type_column}::text AS space_type,
                   {s.space_no_column}::text AS space_no
            FROM {s.space_table}
            WHERE {s.space_parent_id_column}::text=$1
              AND ($2='' OR lower(COALESCE({s.space_type_column}::text,''))=$2)
            ORDER BY {s.space_name_column}::text, {s.space_id_column}::text
            LIMIT $3
        """
        rows = await self.db.fetch(sql, space_id, normalized_type, max_nodes + 1)
        truncated = len(rows) > max_nodes
        return [
            {"node_type": "space", **r, "depth": 1, "path": f"{root['space_name']}/{r['space_name']}", "space_link": None, "metadata": {}}
            for r in rows[:max_nodes]
        ], truncated

    async def _native_tree(self, root_space_id: str, max_depth: int, max_nodes: int) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
        s = self.settings
        root = await self._native_get_by_id(root_space_id)
        if not root:
            raise AssetError(ErrorCode.SPACE_NOT_FOUND, "空间实体不存在")
        sql = f"""
            WITH RECURSIVE tree AS (
                SELECT {s.space_id_column}::text AS space_id,
                       {s.space_parent_id_column}::text AS parent_space_id,
                       {s.space_name_column}::text AS space_name,
                       {s.space_type_column}::text AS space_type,
                       {s.space_no_column}::text AS space_no,
                       0::int AS depth,
                       {s.space_name_column}::text AS path,
                       ARRAY[{s.space_id_column}::text] AS visited
                FROM {s.space_table}
                WHERE {s.space_id_column}::text=$1
                UNION ALL
                SELECT c.{s.space_id_column}::text,
                       c.{s.space_parent_id_column}::text,
                       c.{s.space_name_column}::text,
                       c.{s.space_type_column}::text,
                       c.{s.space_no_column}::text,
                       t.depth+1,
                       t.path || '/' || c.{s.space_name_column}::text,
                       t.visited || c.{s.space_id_column}::text
                FROM {s.space_table} c
                JOIN tree t ON c.{s.space_parent_id_column}::text=t.space_id
                WHERE t.depth < $2
                  AND NOT c.{s.space_id_column}::text = ANY(t.visited)
            )
            SELECT space_id,parent_space_id,space_name,space_type,space_no,depth,path
            FROM tree WHERE depth>0 ORDER BY depth,path,space_id LIMIT $3
        """
        rows = await self.db.fetch(sql, root_space_id, max_depth, max_nodes + 1)
        truncated = len(rows) > max_nodes
        nodes = [{"node_type": "space", **r, "space_link": None, "metadata": {}} for r in rows[:max_nodes]]
        return root, nodes, truncated
