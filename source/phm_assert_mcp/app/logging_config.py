from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

SENSITIVE_KEYS = {"password", "api_key", "authorization", "token", "data_access_token"}


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("***" if any(s in k.lower() for s in SENSITIVE_KEYS) else _sanitize(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(_sanitize(fields))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
