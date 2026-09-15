from __future__ import annotations

import json
import time
from typing import Any

from app.config import Settings
from app.db import DatabaseManager
from app.domain.entities import Candidate


def _metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _candidate(row: dict[str, Any]) -> Candidate:
    known = {
        "entity_type",
        "entity_key",
        "equip_no",
        "point_no",
        "display_name",
        "search_text",
        "metadata",
        "vector_score",
    }
    return Candidate(
        entity_type=str(row.get("entity_type") or ""),
        entity_key=str(row.get("entity_key") or ""),
        display_name=str(row.get("display_name") or ""),
        equip_no=str(row.get("equip_no") or ""),
        point_no=str(row.get("point_no") or ""),
        search_text=str(row.get("search_text") or ""),
        metadata=_metadata(row.get("metadata")),
        vector_score=float(row.get("vector_score") or 0),
        source="embedding",
        extra={key: value for key, value in row.items() if key not in known},
    )


class CatalogRepository:
    """PostgreSQL asset catalog access used by entity resolution.

    Natural-language entity resolution intentionally exposes only exact-code lookup
    and pgvector recall.  The previous exact-name, pg_trgm and hand-written field
    matching paths were removed in 0.4.0 and remain absent in 0.4.1.
    """

    def __init__(self, db: DatabaseManager, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.table = settings.asset_catalog_table
        self._semantic_columns_cache: tuple[float, bool] | None = None

    @staticmethod
    def scope_sql(scope: str) -> str:
        return {
            "equipment": "idx.entity_type = 'equipment'",
            "point": "idx.entity_type = 'point'",
            "equipment_and_point": "idx.entity_type = 'point'",
            "area_aggregate": "idx.entity_type = 'area'",
            "space": "idx.entity_type = 'area'",
            "area": "idx.entity_type = 'area'",
            "line": "idx.entity_type = 'area'",
        }.get(scope, "idx.entity_type IN ('area','equipment','point')")

    async def exact_code(
        self,
        *,
        equip_no: str = "",
        point_no: str = "",
        scope: str = "any",
        active_space_link: str = "",
        area_keywords: list[str] | None = None,
    ) -> list[Candidate]:
        conditions = [self.scope_sql(scope)]
        args: list[Any] = []
        point_target = scope in {"point", "equipment_and_point"}
        if point_no:
            args.append(point_no)
            conditions.append(f"upper(idx.point_no)=upper(${len(args)})")
            if equip_no:
                args.append(equip_no)
                conditions.append(f"upper(idx.equip_no)=upper(${len(args)})")
        elif equip_no and not point_target:
            args.append(equip_no)
            conditions.append(f"upper(idx.equip_no)=upper(${len(args)})")
        else:
            return []
        if active_space_link:
            args.append(active_space_link)
            conditions.append(f"left(trim(both '/' from COALESCE(idx.metadata->>'space_link','')) || '/', length(${len(args)}))=${len(args)}")
        for area in area_keywords or []:
            args.append(area)
            conditions.append(f"strpos(lower(concat_ws('/',idx.metadata->>'space_path',idx.metadata->>'leaf_space_name',idx.metadata->>'area_name',idx.metadata->>'line_name')),lower(${len(args)}))>0")
        sql = f"""
            SELECT idx.entity_type, idx.entity_key, idx.equip_no, idx.point_no,
                   idx.display_name, idx.search_text, idx.metadata,
                   1.0::float AS vector_score
            FROM {self.table} idx
            WHERE {' AND '.join(conditions)}
            ORDER BY idx.entity_key
            LIMIT 20
        """
        candidates = [_candidate(row) for row in await self.db.fetch(sql, *args)]
        for candidate in candidates:
            candidate.source = "code_exact"
        return candidates

    async def lookup_code_tokens(self, tokens: list[str]) -> list[Candidate]:
        """One parameterized lookup; codes acquire a role only through catalog rows."""
        tokens = list(dict.fromkeys(str(x).upper() for x in tokens if x))[:32]
        if not tokens:
            return []
        sql = f"""SELECT idx.entity_type, idx.entity_key, idx.equip_no, idx.point_no,
                    idx.display_name, idx.search_text, idx.metadata, 1.0::float AS vector_score
                  FROM {self.table} idx
                  WHERE (idx.entity_type='equipment' AND upper(idx.equip_no)=ANY($1::text[]))
                     OR (idx.entity_type='point' AND upper(idx.point_no)=ANY($1::text[]))
                  ORDER BY idx.entity_type, idx.entity_key LIMIT 1001"""
        return [_candidate(row) for row in await self.db.fetch(sql, tokens)]


    async def official_exact_name(
        self,
        *,
        term: str,
        scope: str,
        active_space_link: str = "",
        area_keywords: list[str] | None = None,
        limit: int = 40,
    ) -> list[Candidate]:
        """Exact recall on the authoritative catalog display name.

        Official identity must never depend on semantic-enrichment review state.
        Equality is normalized but never substring/fuzzy matching.  If multiple real
        rows share the same official name they remain multiple candidates and the
        normal entity-selection contract applies.
        """
        term = str(term or "").strip()
        if not term:
            return []
        norm = self.settings.asset_normalize_function
        sql = f"""
            SELECT idx.entity_type, idx.entity_key, idx.equip_no, idx.point_no,
                   idx.display_name, idx.search_text, idx.metadata,
                   1.0::float AS vector_score
            FROM {self.table} idx
            WHERE {self.scope_sql(scope)}
              AND {norm}(COALESCE(idx.display_name,''))={norm}($1)
              AND ($2='' OR left(trim(both '/' from COALESCE(idx.metadata->>'space_link','')) || '/',length($2))=$2)
              AND (
                    COALESCE(cardinality($3::text[]),0)=0
                    OR NOT EXISTS (
                        SELECT 1 FROM unnest($3::text[]) AS area_kw
                        WHERE strpos(lower(concat_ws('/',COALESCE(idx.metadata->>'space_path',''),COALESCE(idx.metadata->>'leaf_space_name',''),COALESCE(idx.metadata->>'area_name',''),COALESCE(idx.metadata->>'line_name',''))),lower(area_kw))=0
                    )
                  )
            ORDER BY idx.entity_key
            LIMIT $4
        """
        rows = await self.db.fetch(
            sql,
            term,
            active_space_link,
            list(dict.fromkeys(str(x).strip() for x in (area_keywords or []) if str(x).strip())),
            min(limit, 80),
        )
        candidates = [_candidate(row) for row in rows]
        for candidate in candidates:
            candidate.source = "official_exact_name"
        return candidates


    async def semantic_exact_name(
        self,
        *,
        term: str,
        scope: str,
        active_space_link: str = "",
        area_keywords: list[str] | None = None,
        limit: int = 40,
    ) -> list[Candidate]:
        """Exact reviewed standard-name/alias recall before vector search.

        This is not a keyword/sub-string resolver: equality after the configured
        normalization function is required. Multiple physical assets remain multiple
        candidates and are handled by the normal entity-selection policy.
        """
        term = str(term or '').strip()
        if not term:
            return []
        now = time.monotonic()
        available: bool | None = None
        if self._semantic_columns_cache and self._semantic_columns_cache[0] > now:
            available = self._semantic_columns_cache[1]
        if available is None:
            schema, table = self.table.split('.',1) if '.' in self.table else ('public',self.table)
            rows = await self.db.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_schema=$1 AND table_name=$2",
                schema, table,
            )
            cols = {str(r.get('column_name') or '') for r in rows}
            available = {'normalized_name','search_aliases','semantic_review_status'}.issubset(cols)
            self._semantic_columns_cache = (now+60.0, available)
        if not available:
            return []
        norm = self.settings.asset_normalize_function
        sql=f"""
            SELECT idx.entity_type, idx.entity_key, idx.equip_no, idx.point_no,
                   idx.display_name, idx.search_text, idx.metadata,
                   1.0::float AS vector_score
            FROM {self.table} idx
            WHERE {self.scope_sql(scope)}
              AND upper(COALESCE(idx.semantic_review_status,'')) = ANY($1::text[])
              AND (
                    {norm}(COALESCE(idx.display_name,''))={norm}($2)
                    OR {norm}(COALESCE(idx.normalized_name,''))={norm}($2)
                    OR EXISTS (
                        SELECT 1 FROM unnest(COALESCE(idx.search_aliases, ARRAY[]::text[])) AS a(alias)
                        WHERE {norm}(a.alias)={norm}($2)
                    )
                  )
              AND ($3='' OR left(trim(both '/' from COALESCE(idx.metadata->>'space_link','')) || '/',length($3))=$3)
              AND (
                    COALESCE(cardinality($4::text[]),0)=0
                    OR NOT EXISTS (
                        SELECT 1 FROM unnest($4::text[]) AS area_kw
                        WHERE strpos(lower(concat_ws('/',COALESCE(idx.metadata->>'space_path',''),COALESCE(idx.metadata->>'leaf_space_name',''),COALESCE(idx.metadata->>'area_name',''),COALESCE(idx.metadata->>'line_name',''))),lower(area_kw))=0
                    )
                  )
            ORDER BY idx.entity_key
            LIMIT $5
        """
        rows=await self.db.fetch(sql,['AI_REVIEWED','APPROVED','MODIFIED'],term,active_space_link,list(dict.fromkeys(str(x).strip() for x in (area_keywords or []) if str(x).strip())),min(limit,80))
        candidates=[_candidate(row) for row in rows]
        for candidate in candidates:
            candidate.source='semantic_exact_name_or_alias'
        return candidates

    async def vector_search(
        self,
        *,
        vector_literal: str,
        scope: str,
        equip_no: str = "",
        point_no: str = "",
        active_space_link: str = "",
        area_keywords: list[str] | None = None,
        limit: int = 40,
    ) -> list[Candidate]:
        sql = f"""
            SELECT idx.entity_type, idx.entity_key, idx.equip_no, idx.point_no,
                   idx.display_name, idx.search_text, idx.metadata,
                   (1-(idx.embedding <=> $1::vector))::float AS vector_score
            FROM {self.table} idx
            WHERE {self.scope_sql(scope)}
              AND idx.embedding IS NOT NULL
              AND ($2='' OR upper(COALESCE(idx.equip_no,''))=upper($2))
              AND ($3='' OR upper(COALESCE(idx.point_no,''))=upper($3))
              AND (
                    $4=''
                    OR left(
                        trim(both '/' from COALESCE(idx.metadata->>'space_link','')) || '/',
                        length($4)
                    )=$4
                  )
              AND (
                    COALESCE(cardinality($5::text[]), 0)=0
                    OR NOT EXISTS (
                        SELECT 1
                        FROM unnest($5::text[]) AS area_kw
                        WHERE strpos(
                            lower(concat_ws(
                                '/',
                                COALESCE(idx.metadata->>'space_path',''),
                                COALESCE(idx.metadata->>'space_link',''),
                                COALESCE(idx.metadata->>'company_name',''),
                                COALESCE(idx.metadata->>'plant_name',''),
                                COALESCE(idx.metadata->>'factory_name',''),
                                COALESCE(idx.metadata->>'workshop_name',''),
                                COALESCE(idx.metadata->>'region_name',''),
                                COALESCE(idx.metadata->>'area_name',''),
                                COALESCE(idx.metadata->>'line_name',''),
                                COALESCE(idx.metadata->>'leaf_space_name','')
                            )),
                            lower(area_kw)
                        ) = 0
                    )
                  )
            ORDER BY idx.embedding <=> $1::vector
            LIMIT $6
        """
        rows = await self.db.fetch(
            sql,
            vector_literal,
            equip_no,
            point_no,
            active_space_link,
            list(dict.fromkeys(str(item).strip() for item in (area_keywords or []) if str(item).strip())),
            min(limit, 80),
        )
        return [_candidate(row) for row in rows]
