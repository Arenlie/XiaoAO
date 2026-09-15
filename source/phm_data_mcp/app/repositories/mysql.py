from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pymysql
from pymysql.cursors import DictCursor

from app.config import Settings


class MySqlRepository:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _connect(self):
        return pymysql.connect(
            host=self.settings.mysql_host,
            port=self.settings.mysql_port,
            user=self.settings.mysql_user,
            password=self.settings.mysql_password,
            database=self.settings.mysql_database,
            charset="utf8mb4",
            cursorclass=DictCursor,
            connect_timeout=self.settings.query_timeout_seconds,
            read_timeout=self.settings.query_timeout_seconds,
            write_timeout=self.settings.query_timeout_seconds,
            autocommit=True,
        )

    def ping(self) -> bool:
        with self._connect() as conn:
            conn.ping(reconnect=False)
        return True

    @staticmethod
    def _json_value(value: Any) -> Any:
        if isinstance(value, (datetime, date)):
            return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

    def query(self, sql: str, params: list[Any] | tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute("START TRANSACTION READ ONLY")
                try:
                    cursor.execute(sql, params)
                    rows = list(cursor.fetchall())
                finally:
                    conn.rollback()
        return [{key: self._json_value(value) for key, value in row.items()} for row in rows]
