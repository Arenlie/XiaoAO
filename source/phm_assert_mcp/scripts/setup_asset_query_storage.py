#!/usr/bin/env python3
"""Create only the additive asset-query snapshot tables, leaving the catalog untouched."""
import asyncio
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import get_settings
from app.db import DatabaseManager
from app.services.asset_collection_engine import AssetCollectionEngine


async def main():
    settings=get_settings();db=DatabaseManager(settings)
    try:
        engine=AssetCollectionEngine(db,settings)
        await engine.migrate()
        if not await engine.ready():raise RuntimeError("storage contract not ready")
        print("资产集合暂存表已准备完成，资产目录未修改。")
    finally:
        await db.close()


if __name__=="__main__":
    try:asyncio.run(main())
    except Exception as exc:
        print("暂存表准备失败："+type(exc).__name__+"。请检查数据库权限；未输出连接凭据。",file=sys.stderr)
        sys.exit(1)
