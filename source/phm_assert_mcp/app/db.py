from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

try:
    import asyncpg
except ModuleNotFoundError:  # Allows schema/unit tooling to import without optional runtime deps installed.
    asyncpg = None  # type: ignore[assignment]

from app.config import Settings, validate_identifier
from app.errors import DatabaseError

logger = logging.getLogger(__name__)


def _require_asyncpg() -> None:
    if asyncpg is None:
        raise RuntimeError("asyncpg is not installed; install project runtime dependencies first")


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


class DatabaseManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._pool: asyncpg.Pool | None = None
        self._lock: asyncio.Lock | None = None

    async def _connect(self) -> asyncpg.Connection:
        _require_asyncpg()
        return await asyncpg.connect(
            host=self.settings.postgres_host,
            port=self.settings.postgres_port,
            database=self.settings.postgres_database,
            user=self.settings.postgres_user,
            password=self.settings.postgres_password,
            timeout=self.settings.postgres_connect_timeout,
            command_timeout=self.settings.postgres_command_timeout,
        )

    async def get_pool(self) -> asyncpg.Pool:
        _require_asyncpg()
        if self._pool is not None:
            return self._pool
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._pool is None:
                try:
                    self._pool = await asyncpg.create_pool(
                        host=self.settings.postgres_host,
                        port=self.settings.postgres_port,
                        database=self.settings.postgres_database,
                        user=self.settings.postgres_user,
                        password=self.settings.postgres_password,
                        min_size=self.settings.postgres_pool_min_size,
                        max_size=self.settings.postgres_pool_max_size,
                        timeout=self.settings.postgres_connect_timeout,
                        command_timeout=self.settings.postgres_command_timeout,
                        init=_init_connection,
                    )
                except Exception as exc:
                    logger.exception("postgres_pool_create_failed")
                    raise DatabaseError(
                        operator_message=f"postgres pool create failed: {type(exc).__name__}: {exc}"
                    ) from exc
        return self._pool

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        try:
            pool = await self.get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(sql, *args)
                return [dict(row) for row in rows]
        except DatabaseError:
            raise
        except Exception as exc:
            logger.exception("postgres_fetch_failed")
            raise DatabaseError(
                operator_message=f"postgres fetch failed: {type(exc).__name__}: {exc}"
            ) from exc

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        try:
            pool = await self.get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(sql, *args)
                return dict(row) if row else None
        except DatabaseError:
            raise
        except Exception as exc:
            logger.exception("postgres_fetchrow_failed")
            raise DatabaseError(
                operator_message=f"postgres fetchrow failed: {type(exc).__name__}: {exc}"
            ) from exc

    async def execute(self, sql: str, *args: Any) -> str:
        try:
            pool = await self.get_pool()
            async with pool.acquire() as conn:
                return await conn.execute(sql, *args)
        except Exception as exc:
            logger.exception("postgres_execute_failed")
            raise DatabaseError(
                operator_message=f"postgres execute failed: {type(exc).__name__}: {exc}"
            ) from exc

    async def ping(self) -> bool:
        try:
            pool = await self.get_pool()
            async with pool.acquire() as conn:
                return bool(await conn.fetchval("SELECT 1"))
        except Exception:
            return False

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


async def preflight_database(settings: Settings) -> dict[str, Any]:
    """Fail-fast validation before the MCP server starts.

    The raw space tree schema was not present in the Dify YML, therefore only
    configured identifiers are validated. Catalog columns/functions below are
    directly evidenced by the uploaded production YML.
    """
    _require_asyncpg()
    conn: asyncpg.Connection | None = None
    try:
        conn = await asyncpg.connect(
            host=settings.postgres_host,
            port=settings.postgres_port,
            database=settings.postgres_database,
            user=settings.postgres_user,
            password=settings.postgres_password,
            timeout=settings.postgres_connect_timeout,
            command_timeout=settings.postgres_command_timeout,
        )
        catalog = validate_identifier(settings.asset_catalog_table, qualified=True)
        schema, table = catalog.split(".", 1) if "." in catalog else ("public", catalog)
        existing = await conn.fetch(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema=$1 AND table_name=$2
            """,
            schema,
            table,
        )
        columns = {r["column_name"] for r in existing}
        required = {"entity_type", "entity_key", "equip_no", "point_no", "display_name", "search_text", "metadata"}
        missing = sorted(required - columns)
        if missing:
            raise RuntimeError(f"{catalog} missing required columns: {missing}")

        normalize_fn = validate_identifier(settings.asset_normalize_function, qualified=True)
        # Safe because identifier is configuration-only and validated above.
        try:
            await conn.fetchval(f"SELECT {normalize_fn}('Test')")
        except Exception as exc:
            raise RuntimeError(f"normalize function {normalize_fn} is unavailable") from exc

        if settings.embedding_configured:
            if "embedding" not in columns:
                raise RuntimeError(f"{catalog}.embedding is required when EMBEDDING_ENABLED=true")
            vector_extension = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector')")
            if not vector_extension:
                raise RuntimeError("pgvector extension is required when EMBEDDING_ENABLED=true")
            sample_dimension = await conn.fetchval(
                f"SELECT vector_dims(embedding) FROM {catalog} "
                "WHERE embedding IS NOT NULL LIMIT 1"
            )
            if sample_dimension is not None and int(sample_dimension) != settings.embedding_dimension:
                raise RuntimeError(
                    f"{catalog}.embedding contains {sample_dimension}-dimensional vectors; "
                    f"configured model requires {settings.embedding_dimension}"
                )
            embedding_coverage = await conn.fetchrow(
                f"""
                SELECT count(*)::int AS total,
                       count(*) FILTER (WHERE embedding IS NOT NULL)::int AS embedded
                FROM {catalog}
                """
            )
        else:
            embedding_coverage = None

        if settings.asset_hierarchy_backend == "native_recursive":
            native = validate_identifier(settings.space_table, qualified=True)
            nschema, ntable = native.split(".", 1) if "." in native else ("public", native)
            rows = await conn.fetch(
                "SELECT column_name FROM information_schema.columns WHERE table_schema=$1 AND table_name=$2",
                nschema,
                ntable,
            )
            native_cols = {r["column_name"] for r in rows}
            required_native = {
                settings.space_id_column,
                settings.space_parent_id_column,
                settings.space_name_column,
                settings.space_type_column,
                settings.space_no_column,
            }
            missing_native = sorted(c for c in required_native if c not in native_cols)
            if missing_native:
                raise RuntimeError(f"{native} missing configured columns: {missing_native}")

        semantic_required = {"normalized_name","search_aliases","tag_names","tag_codes","semantic_keywords","semantic_review_status"}
        semantic_missing = sorted(semantic_required - columns)
        semantic_rows = None
        if not semantic_missing:
            semantic_rows = await conn.fetchrow(
                f"SELECT count(*)::int AS reviewed_rows, count(*) FILTER (WHERE entity_type='equipment')::int AS reviewed_equipment FROM {catalog} WHERE upper(COALESCE(semantic_review_status,''))=ANY($1::text[]) AND COALESCE(cardinality(tag_codes),0)>0",
                ["AI_REVIEWED","APPROVED","MODIFIED"],
            )

        return {
            "postgres": "UP",
            "catalog": catalog,
            "catalog_columns": sorted(columns),
            "hierarchy_backend": settings.asset_hierarchy_backend,
            "embedding_coverage": dict(embedding_coverage) if embedding_coverage else None,
            "semantic_tags": {"available": not semantic_missing, "missing_columns": semantic_missing, "coverage": dict(semantic_rows) if semantic_rows else None},
        }
    finally:
        if conn is not None:
            await conn.close()
