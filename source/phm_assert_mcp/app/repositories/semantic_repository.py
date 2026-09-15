from __future__ import annotations

import time
from typing import Any

from app.config import Settings
from app.db import DatabaseManager
from app.repositories.space_repository import SpaceRepository


TRUSTED_SEMANTIC_STATUSES = ("AI_REVIEWED", "APPROVED", "MODIFIED")
REQUIRED_SEMANTIC_COLUMNS = {
    "normalized_name", "search_aliases", "tag_names", "tag_codes",
    "semantic_keywords", "semantic_review_status",
}


class SemanticRepository:
    """Read-only access to reviewed offline asset semantics.

    Semantic labels are facts only after the offline import/review step. Runtime LLMs
    may map a user phrase to one of these existing labels, but cannot invent labels or
    decide whether an asset belongs to a category.
    """

    def __init__(self, db: DatabaseManager, settings: Settings, spaces: SpaceRepository) -> None:
        self.db = db
        self.settings = settings
        self.spaces = spaces
        self.table = settings.asset_catalog_table
        self._capability: tuple[float, dict[str, Any]] | None = None
        self._dictionary: dict[str, tuple[float, list[dict[str, str]]]] = {}

    async def capability(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._capability and self._capability[0] > now:
            return dict(self._capability[1])
        schema, table = self.table.split('.', 1) if '.' in self.table else ('public', self.table)
        rows = await self.db.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_schema=$1 AND table_name=$2",
            schema, table,
        )
        columns = {str(row.get('column_name') or '') for row in rows}
        missing = sorted(REQUIRED_SEMANTIC_COLUMNS - columns)
        payload = {
            'available': not missing,
            'missing_columns': missing,
            'trusted_statuses': list(TRUSTED_SEMANTIC_STATUSES),
        }
        if not missing:
            row = await self.db.fetchrow(
                f"""SELECT count(*)::int AS reviewed_rows,
                           count(*) FILTER (WHERE entity_type='equipment')::int AS reviewed_equipment,
                           count(*) FILTER (WHERE entity_type='area')::int AS reviewed_areas
                    FROM {self.table}
                    WHERE upper(COALESCE(semantic_review_status,'')) = ANY($1::text[])
                      AND COALESCE(cardinality(tag_codes),0) > 0""",
                list(TRUSTED_SEMANTIC_STATUSES),
            ) or {}
            payload.update({k: int(row.get(k) or 0) for k in ('reviewed_rows','reviewed_equipment','reviewed_areas')})
        self._capability = (now + 60.0, payload)
        return dict(payload)

    async def tag_dictionary(self, entity_type: str) -> list[dict[str, str]]:
        cached = self._dictionary.get(entity_type)
        now = time.monotonic()
        if cached and cached[0] > now:
            return [dict(x) for x in cached[1]]
        capability = await self.capability()
        if not capability.get('available'):
            return []
        rows = await self.db.fetch(
            f"""
            SELECT DISTINCT idx.tag_codes[pos] AS tag_code, idx.tag_names[pos] AS tag_name
            FROM {self.table} idx
            CROSS JOIN LATERAL generate_subscripts(idx.tag_codes, 1) AS pos
            WHERE idx.entity_type=$1
              AND upper(COALESCE(idx.semantic_review_status,'')) = ANY($2::text[])
              AND pos <= COALESCE(cardinality(idx.tag_names),0)
              AND COALESCE(idx.tag_codes[pos],'') <> ''
              AND COALESCE(idx.tag_names[pos],'') <> ''
            ORDER BY tag_name, tag_code
            """,
            entity_type,
            list(TRUSTED_SEMANTIC_STATUSES),
        )
        result = [
            {'tag_code': str(r.get('tag_code') or ''), 'tag_name': str(r.get('tag_name') or '')}
            for r in rows if r.get('tag_code') and r.get('tag_name')
        ]
        self._dictionary[entity_type] = (now + 300.0, result)
        return [dict(x) for x in result]

    async def _scope(self, space_id: str, recursive: bool) -> tuple[str, list[str]]:
        space = await self.spaces.get_by_id(space_id)
        if not space:
            return '', []
        link = str(space.get('space_link') or '').strip('/')
        if self.settings.asset_hierarchy_backend == 'catalog_path':
            return (link + '/' if link else ''), [space_id]
        scope_ids = [space_id]
        if recursive:
            _, descendants, _ = await self.spaces.get_tree(
                space_id, self.settings.asset_tree_max_depth, self.settings.asset_tree_max_nodes
            )
            scope_ids.extend(str(n.get('space_id')) for n in descendants if n.get('space_id'))
        return '', list(dict.fromkeys(scope_ids))

    async def count_equipment(self, *, space_id: str, recursive: bool, tag_codes: list[str]) -> int:
        link, scope_ids = await self._scope(space_id, recursive)
        if not scope_ids:
            return 0
        tags = list(dict.fromkeys(str(x).strip() for x in tag_codes if str(x).strip()))
        if self.settings.asset_hierarchy_backend == 'catalog_path' and link:
            row = await self.db.fetchrow(
                f"""SELECT count(*)::int AS n FROM {self.table} idx
                    WHERE idx.entity_type='equipment'
                      AND (($4::boolean AND left(trim(both '/' from COALESCE(idx.metadata->>'space_link','')) || '/',length($1))=$1)
                           OR (NOT $4::boolean AND (idx.metadata->>'space_id'=$2 OR idx.metadata->>'leaf_space_id'=$2)))
                      AND upper(COALESCE(idx.semantic_review_status,'')) = ANY($5::text[])
                      AND idx.tag_codes @> $3::text[]""",
                link, space_id, tags, recursive, list(TRUSTED_SEMANTIC_STATUSES),
            ) or {}
        else:
            row = await self.db.fetchrow(
                f"""SELECT count(*)::int AS n FROM {self.table} idx
                    WHERE idx.entity_type='equipment'
                      AND (idx.metadata->>'space_id'=ANY($1::text[]) OR idx.metadata->>'leaf_space_id'=ANY($1::text[]))
                      AND upper(COALESCE(idx.semantic_review_status,'')) = ANY($3::text[])
                      AND idx.tag_codes @> $2::text[]""",
                scope_ids, tags, list(TRUSTED_SEMANTIC_STATUSES),
            ) or {}
        return int(row.get('n') or 0)

    async def query_equipment_page(self, *, space_id: str, recursive: bool, tag_codes: list[str], limit: int, offset: int) -> list[dict[str, Any]]:
        link, scope_ids = await self._scope(space_id, recursive)
        if not scope_ids:
            return []
        tags = list(dict.fromkeys(str(x).strip() for x in tag_codes if str(x).strip()))
        select = """idx.entity_key, idx.equip_no, idx.display_name, idx.metadata,
                    idx.normalized_name, idx.search_aliases, idx.tag_names, idx.tag_codes,
                    idx.semantic_keywords, idx.semantic_profile_summary,
                    idx.semantic_confidence, idx.semantic_review_status, idx.taxonomy_version"""
        if self.settings.asset_hierarchy_backend == 'catalog_path' and link:
            rows = await self.db.fetch(
                f"""SELECT {select} FROM {self.table} idx
                    WHERE idx.entity_type='equipment'
                      AND (($6::boolean AND left(trim(both '/' from COALESCE(idx.metadata->>'space_link','')) || '/',length($1))=$1)
                           OR (NOT $6::boolean AND (idx.metadata->>'space_id'=$2 OR idx.metadata->>'leaf_space_id'=$2)))
                      AND upper(COALESCE(idx.semantic_review_status,'')) = ANY($7::text[])
                      AND idx.tag_codes @> $3::text[]
                    ORDER BY idx.metadata->>'space_path', COALESCE(idx.metadata->>'equip_name',idx.display_name), idx.equip_no, idx.entity_key
                    LIMIT $4 OFFSET $5""",
                link, space_id, tags, int(limit), int(offset), recursive, list(TRUSTED_SEMANTIC_STATUSES),
            )
        else:
            rows = await self.db.fetch(
                f"""SELECT {select} FROM {self.table} idx
                    WHERE idx.entity_type='equipment'
                      AND (idx.metadata->>'space_id'=ANY($1::text[]) OR idx.metadata->>'leaf_space_id'=ANY($1::text[]))
                      AND upper(COALESCE(idx.semantic_review_status,'')) = ANY($5::text[])
                      AND idx.tag_codes @> $2::text[]
                    ORDER BY idx.metadata->>'space_path', COALESCE(idx.metadata->>'equip_name',idx.display_name), idx.equip_no, idx.entity_key
                    LIMIT $3 OFFSET $4""",
                scope_ids, tags, int(limit), int(offset), list(TRUSTED_SEMANTIC_STATUSES),
            )
        return [self._equipment_view(row) for row in rows]

    async def count_spaces(self, *, space_id: str, recursive: bool, tag_codes: list[str], target_space_type: str | None = None) -> int:
        # Areas use the same catalog path semantics; parent/descendant determination is authoritative metadata.
        root = await self.spaces.get_by_id(space_id)
        if not root:
            return 0
        root_link = str(root.get('space_link') or '').strip('/')
        tags = list(dict.fromkeys(str(x).strip() for x in tag_codes if str(x).strip()))
        if not root_link:
            return 0
        prefix = root_link + '/'
        type_term = str(target_space_type or '').strip()
        row = await self.db.fetchrow(
            f"""SELECT count(*)::int AS n FROM {self.table} idx
                WHERE idx.entity_type='area' AND COALESCE(idx.metadata->>'space_id','') <> $2
                  AND (($5::boolean AND left(trim(both '/' from COALESCE(idx.metadata->>'space_link','')) || '/',length($1))=$1)
                       OR (NOT $5::boolean AND (idx.metadata->>'parent_space_id'=$3 OR idx.metadata->>'space_parent_id'=$3)))
                  AND upper(COALESCE(idx.semantic_review_status,'')) = ANY($6::text[])
                  AND idx.tag_codes @> $4::text[]
                  AND ($7='' OR lower(COALESCE(idx.metadata->>'leaf_space_type',idx.metadata->>'space_type','')) LIKE '%'||lower($7)||'%')""",
            prefix, space_id, space_id, tags, recursive,
            list(TRUSTED_SEMANTIC_STATUSES), type_term,
        ) or {}
        return int(row.get('n') or 0)

    async def query_space_page(self, *, space_id: str, recursive: bool, tag_codes: list[str], target_space_type: str | None, limit: int, offset: int) -> list[dict[str, Any]]:
        root = await self.spaces.get_by_id(space_id)
        if not root:
            return []
        root_link = str(root.get('space_link') or '').strip('/')
        if not root_link:
            return []
        prefix = root_link + '/'
        tags = list(dict.fromkeys(str(x).strip() for x in tag_codes if str(x).strip()))
        type_term = str(target_space_type or '').strip()
        rows = await self.db.fetch(
            f"""SELECT idx.entity_key, idx.display_name, idx.metadata,
                       idx.normalized_name, idx.search_aliases, idx.tag_names, idx.tag_codes,
                       idx.semantic_keywords, idx.semantic_profile_summary,
                       idx.semantic_confidence, idx.semantic_review_status, idx.taxonomy_version
                FROM {self.table} idx
                WHERE idx.entity_type='area' AND COALESCE(idx.metadata->>'space_id','') <> $2
                  AND (($5::boolean AND left(trim(both '/' from COALESCE(idx.metadata->>'space_link','')) || '/',length($1))=$1)
                       OR (NOT $5::boolean AND (idx.metadata->>'parent_space_id'=$3 OR idx.metadata->>'space_parent_id'=$3)))
                  AND upper(COALESCE(idx.semantic_review_status,'')) = ANY($6::text[])
                  AND idx.tag_codes @> $4::text[]
                  AND ($7='' OR lower(COALESCE(idx.metadata->>'leaf_space_type',idx.metadata->>'space_type','')) LIKE '%'||lower($7)||'%')
                ORDER BY idx.metadata->>'space_path', idx.display_name, idx.entity_key LIMIT $8 OFFSET $9""",
            prefix, space_id, space_id, tags, recursive,
            list(TRUSTED_SEMANTIC_STATUSES), type_term, int(limit), int(offset),
        )
        return [self._space_view(row) for row in rows]

    async def equipment_semantics_by_no(self, equip_no: str) -> dict[str, Any]:
        capability = await self.capability()
        if not capability.get('available'):
            return {}
        row = await self.db.fetchrow(
            f"""SELECT normalized_name, search_aliases, tag_names, tag_codes,
                       semantic_keywords, semantic_profile_summary, semantic_confidence,
                       semantic_review_status, taxonomy_version
                FROM {self.table}
                WHERE entity_type='equipment' AND upper(COALESCE(equip_no,''))=upper($1)
                  AND upper(COALESCE(semantic_review_status,'')) = ANY($2::text[])
                ORDER BY entity_key LIMIT 1""",
            equip_no, list(TRUSTED_SEMANTIC_STATUSES),
        )
        if not row:
            return {}
        return {
            'normalized_name': row.get('normalized_name'),
            'search_aliases': list(row.get('search_aliases') or []),
            'tag_names': list(row.get('tag_names') or []),
            'tag_codes': list(row.get('tag_codes') or []),
            'semantic_keywords': list(row.get('semantic_keywords') or []),
            'semantic_profile_summary': row.get('semantic_profile_summary') or {},
            'semantic_confidence': row.get('semantic_confidence'),
            'semantic_review_status': row.get('semantic_review_status'),
            'taxonomy_version': row.get('taxonomy_version'),
        }

    @staticmethod
    def _equipment_view(row: dict[str, Any]) -> dict[str, Any]:
        m = row.get('metadata') if isinstance(row.get('metadata'), dict) else {}
        return {
            'equip_id': str(m.get('equip_id') or row.get('entity_key') or '') or None,
            'equip_no': str(row.get('equip_no') or m.get('equip_no') or '') or None,
            'equip_name': str(m.get('equip_name') or row.get('display_name') or '') or None,
            'equipment_type': str(m.get('equipment_type') or m.get('equip_type') or '') or None,
            'space_id': str(m.get('space_id') or m.get('leaf_space_id') or '') or None,
            'space_name': str(m.get('leaf_space_name') or m.get('area_name') or m.get('line_name') or '') or None,
            'space_path': str(m.get('space_path') or '') or None,
            'space_link': str(m.get('space_link') or '') or None,
            'normalized_name': row.get('normalized_name'),
            'search_aliases': list(row.get('search_aliases') or []),
            'tag_names': list(row.get('tag_names') or []),
            'tag_codes': list(row.get('tag_codes') or []),
            'semantic_keywords': list(row.get('semantic_keywords') or []),
            'semantic_confidence': row.get('semantic_confidence'),
            'semantic_review_status': row.get('semantic_review_status'),
            'taxonomy_version': row.get('taxonomy_version'),
            'semantic_profile_summary': row.get('semantic_profile_summary') or {},
            'metadata': m,
        }

    @staticmethod
    def _space_view(row: dict[str, Any]) -> dict[str, Any]:
        m = row.get('metadata') if isinstance(row.get('metadata'), dict) else {}
        return {
            'entity_type': 'space',
            'space_id': str(m.get('space_id') or m.get('leaf_space_id') or row.get('entity_key') or '') or None,
            'space_name': str(m.get('leaf_space_name') or row.get('display_name') or m.get('area_name') or '') or None,
            'space_type': str(m.get('leaf_space_type') or m.get('space_type') or '') or None,
            'space_no': str(m.get('space_number') or m.get('space_code') or '') or None,
            'space_path': str(m.get('space_path') or '') or None,
            'space_link': str(m.get('space_link') or '') or None,
            'parent_space_id': str(m.get('parent_space_id') or m.get('space_parent_id') or '') or None,
            'normalized_name': row.get('normalized_name'),
            'search_aliases': list(row.get('search_aliases') or []),
            'tag_names': list(row.get('tag_names') or []),
            'tag_codes': list(row.get('tag_codes') or []),
            'semantic_keywords': list(row.get('semantic_keywords') or []),
            'semantic_confidence': row.get('semantic_confidence'),
            'semantic_review_status': row.get('semantic_review_status'),
            'taxonomy_version': row.get('taxonomy_version'),
            'semantic_profile_summary': row.get('semantic_profile_summary') or {},
            'metadata': m,
        }
