from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg_pool import ConnectionPool


class AuditRepository:
    def __init__(self, dsn: str, retention_days: int):
        self.dsn = dsn
        self.retention_days = max(1, retention_days)
        self.pool: ConnectionPool | None = None

    def start(self) -> None:
        self.pool = ConnectionPool(self.dsn, min_size=1, max_size=4, open=True)
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tool_calls (
                    id BIGSERIAL PRIMARY KEY,
                    tool_name TEXT NOT NULL,
                    started_at TIMESTAMPTZ NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    success BOOLEAN NOT NULL,
                    input_summary JSONB NOT NULL,
                    output_summary JSONB,
                    error TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS model_calls (
                    id BIGSERIAL PRIMARY KEY,
                    started_at TIMESTAMPTZ NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    success BOOLEAN NOT NULL,
                    model TEXT,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    error TEXT
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_started_at ON tool_calls(started_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_model_calls_started_at ON model_calls(started_at)")
            cur.execute("DELETE FROM tool_calls WHERE started_at < %s", (datetime.now(timezone.utc) - timedelta(days=self.retention_days),))
            cur.execute("DELETE FROM model_calls WHERE started_at < %s", (datetime.now(timezone.utc) - timedelta(days=self.retention_days),))
            conn.commit()

    def close(self) -> None:
        if self.pool:
            self.pool.close()
            self.pool = None

    def ping(self) -> bool:
        if not self.pool:
            return False
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            return cur.fetchone()[0] == 1

    def log_tool(self, tool_name: str, started_at: datetime, duration_ms: int, success: bool, input_summary: dict[str, Any], output_summary: dict[str, Any] | None, error: str | None) -> None:
        if not self.pool:
            return
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tool_calls(tool_name,started_at,duration_ms,success,input_summary,output_summary,error) VALUES(%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)",
                (tool_name, started_at, duration_ms, success, json.dumps(input_summary, ensure_ascii=False), json.dumps(output_summary, ensure_ascii=False) if output_summary is not None else None, error),
            )
            conn.commit()

    def log_model(self, started_at: datetime, duration_ms: int, success: bool, model: str | None, input_tokens: int | None, output_tokens: int | None, error: str | None) -> None:
        if not self.pool:
            return
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO model_calls(started_at,duration_ms,success,model,input_tokens,output_tokens,error) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (started_at, duration_ms, success, model, input_tokens, output_tokens, error),
            )
            conn.commit()
