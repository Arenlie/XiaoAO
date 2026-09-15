from __future__ import annotations

import json
import logging
from typing import Any

from psycopg_pool import ConnectionPool

from app.config import Settings

logger = logging.getLogger(__name__)


class AuditRepository:
    """Small PostgreSQL audit store. Raw waveform/trend payloads are never written here."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.pool: ConnectionPool | None = None
        self._writes = 0

    def start(self) -> None:
        if not self.settings.audit_enabled:
            return
        try:
            self.pool = ConnectionPool(self.settings.audit_database_url, min_size=1, max_size=4, open=True)
            with self.pool.connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS tool_calls (
                        id BIGSERIAL PRIMARY KEY,
                        tool_name TEXT NOT NULL,
                        started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        duration_ms INTEGER NOT NULL,
                        success BOOLEAN NOT NULL,
                        input_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                        output_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
                        error TEXT
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_started_at ON tool_calls(started_at DESC)")
                conn.commit()
        except Exception as exc:
            logger.warning("Audit database unavailable; tool execution will continue without audit: %s", exc)
            if self.pool:
                self.pool.close()
            self.pool = None

    def close(self) -> None:
        if self.pool:
            self.pool.close()
            self.pool = None

    def ping(self) -> bool:
        if not self.settings.audit_enabled:
            return True
        if not self.pool:
            return False
        try:
            with self.pool.connection() as conn:
                return conn.execute("SELECT 1").fetchone()[0] == 1
        except Exception:
            return False

    def _json_for_log(self, value: dict[str, Any]) -> str:
        raw = json.dumps(value, ensure_ascii=False, default=str)
        size = len(raw.encode("utf-8"))
        if size <= self.settings.audit_max_json_bytes:
            return raw
        return json.dumps({"truncated": True, "original_bytes": size}, ensure_ascii=False)

    def record(self, tool_name: str, duration_ms: int, success: bool, input_data: dict[str, Any], output_summary: dict[str, Any], error: str | None) -> None:
        if not self.pool:
            return
        try:
            with self.pool.connection() as conn:
                conn.execute(
                    "INSERT INTO tool_calls(tool_name,duration_ms,success,input_json,output_summary,error) VALUES(%s,%s,%s,%s::jsonb,%s::jsonb,%s)",
                    (tool_name, duration_ms, success, self._json_for_log(input_data), self._json_for_log(output_summary), error[:2000] if error else None),
                )
                conn.commit()
            self._writes += 1
            if self._writes % 100 == 0:
                self.cleanup()
        except Exception as exc:
            logger.warning("Failed to write MCP audit log: %s", exc)

    def cleanup(self) -> None:
        if not self.pool:
            return
        with self.pool.connection() as conn:
            conn.execute("DELETE FROM tool_calls WHERE started_at < now() - (%s * interval '1 day')", (self.settings.audit_retention_days,))
            conn.execute(
                """
                DELETE FROM tool_calls
                WHERE id IN (
                    SELECT id FROM tool_calls
                    ORDER BY id DESC
                    OFFSET %s
                )
                """,
                (self.settings.audit_max_rows,),
            )
            conn.commit()
