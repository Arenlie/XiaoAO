#!/usr/bin/env python3
"""Add exact lookup indexes without changing recall/scoring or using approximate ANN."""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import get_settings, validate_identifier
from app.db import DatabaseManager


def index_specs(settings):
    table = validate_identifier(settings.asset_catalog_table, qualified=True)
    norm = validate_identifier(settings.asset_normalize_function, qualified=True)
    suffix = hashlib.sha256(table.encode()).hexdigest()[:8]
    return [(f"phm_v11_equip_{suffix}", "entity_type, upper(equip_no)"),
            (f"phm_v11_point_{suffix}", "entity_type, upper(point_no)"),
            (f"phm_v11_name_{suffix}", f"entity_type, {norm}(COALESCE(display_name,''))")]


async def main(apply):
    settings = get_settings()
    specs = index_specs(settings)
    if not apply:
        for name, expression in specs:
            print(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {settings.asset_catalog_table} ({expression});")
        return
    db = DatabaseManager(settings)
    conn = await db._connect()
    try:
        await conn.execute("SET lock_timeout = '5s'")
        # Index building may take minutes on a large catalog; no model/request timeout.
        for name, expression in specs:
            valid = await conn.fetchval("SELECT i.indisvalid FROM pg_index i WHERE i.indexrelid=to_regclass($1)",
                                        settings.asset_catalog_table.rsplit('.',1)[0] + '.' + name if '.' in settings.asset_catalog_table else name)
            if valid is False:
                schema = settings.asset_catalog_table.rsplit('.',1)[0] if '.' in settings.asset_catalog_table else 'public'
                await conn.execute(f"DROP INDEX CONCURRENTLY {schema}.{name}", timeout=3600)
            await conn.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {settings.asset_catalog_table} ({expression})", timeout=3600)
            print("READY", name)
        await conn.execute(f"ANALYZE {settings.asset_catalog_table}", timeout=3600)
    finally:
        await conn.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    asyncio.run(main(args.apply))
