#!/usr/bin/env bash
set -Eeuo pipefail
BASE="${PHM_BASE:-/home/chaos/program}"
APP="$BASE/conversation_management"
PY="$APP/.venv/bin/python"
CID="${1:-}"

if [[ ! -x "$PY" ]]; then
  echo "错误：找不到 $PY" >&2
  exit 2
fi

cd "$APP"
export PHM_EVIDENCE_VERIFY_CONVERSATION_ID="$CID"
"$PY" - <<'PY'
from __future__ import annotations
import asyncio
import os
from uuid import UUID
from sqlalchemy import text
from app.config import get_settings
from app.database import Database
from app.security_context import rls_bypass_scope

EXPECTED_HEAD = "0003_evidence_fabric_v1"
TABLES = [
    "conversation_topics",
    "conversation_context_slots",
    "evidence_objects",
    "topic_evidence_refs",
    "evidence_lineage_edges",
    "task_evidence_refs",
]

async def main():
    settings = get_settings()
    db = Database(settings)
    try:
        with rls_bypass_scope():
            async with db.session_factory() as session:
                rev = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()
                if rev != EXPECTED_HEAD:
                    raise SystemExit(f"FAIL alembic_revision expected={EXPECTED_HEAD} actual={rev}")
                rows = (await session.execute(text("""
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema='public'
                    ORDER BY table_name
                """))).scalars().all()
                missing = sorted(set(TABLES) - set(rows))
                if missing:
                    raise SystemExit(f"FAIL missing_tables={missing}")
                counts = {}
                for table in TABLES:
                    counts[table] = int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())
                print(f"PASS alembic_revision={rev}")
                print("PASS evidence_tables=" + ",".join(TABLES))
                print("COUNTS " + " ".join(f"{k}={v}" for k,v in counts.items()))
                cid = os.environ.get("PHM_EVIDENCE_VERIFY_CONVERSATION_ID", "").strip()
                if cid:
                    conversation_id = UUID(cid)
                    slots = (await session.execute(text("""
                        SELECT slot_no,topic_id,activated_at FROM conversation_context_slots
                        WHERE conversation_id=:cid ORDER BY slot_no
                    """), {"cid": conversation_id})).mappings().all()
                    topics = (await session.execute(text("""
                        SELECT topic_id,title,status,version,updated_at FROM conversation_topics
                        WHERE conversation_id=:cid ORDER BY updated_at DESC
                    """), {"cid": conversation_id})).mappings().all()
                    ev_count = int((await session.execute(text(
                        "SELECT count(*) FROM evidence_objects WHERE conversation_id=:cid"
                    ), {"cid": conversation_id})).scalar_one())
                    print(f"CONVERSATION {conversation_id} topics={len(topics)} evidence={ev_count}")
                    print("HOT_SLOTS " + "; ".join(f"{r['slot_no']}={r['topic_id']}" for r in slots))
                    for r in topics[:10]:
                        print(f"TOPIC {r['topic_id']} status={r['status']} version={r['version']} title={r['title']}")
    finally:
        await db.close()

asyncio.run(main())
PY

ASSET_PY="$BASE/phm_assert_mcp/.venv/bin/python"
if [[ -x "$ASSET_PY" ]]; then
  cd "$BASE/phm_assert_mcp"
  "$ASSET_PY" - <<'PY'
from app.config import get_settings
s=get_settings()
print(f"ASSET snapshot_freshness_ttl_seconds={s.asset_query_snapshot_ttl_seconds}")
print(f"ASSET snapshot_history_retention_seconds={s.asset_query_snapshot_retention_seconds}")
print(f"ASSET snapshot_owner_limit={s.asset_query_snapshot_owner_limit}")
PY
fi
