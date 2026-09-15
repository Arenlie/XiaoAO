from __future__ import annotations

import json
from typing import Any

SENSITIVE_KEYS = {
    "data_access_token",
    "access_token",
    "refresh_token",
    "token",
    "authorization",
    "api_key",
    "apikey",
    "password",
    "passwd",
    "secret",
    "client_secret",
}

_REDACTED = "***REDACTED***"
_MAX_STRING = 32768
_MAX_LIST_ITEMS = 100
_MAX_DICT_ITEMS = 200


def _is_sensitive_key(key: Any) -> bool:
    normalized = str(key or "").strip().lower().replace("-", "_")
    return normalized in SENSITIVE_KEYS or normalized.endswith("_token") or normalized.endswith("_secret")


def sanitize_event_payload(value: Any, *, max_depth: int = 12) -> Any:
    """Recursively redact credentials and cap process-event payload size.

    This function is used before Redis publication and PostgreSQL persistence, so
    secrets never reach the browser, event stream, process-event table or logs that
    serialize these event payloads.
    """

    def walk(item: Any, depth: int) -> Any:
        if depth > max_depth:
            return "<max-depth>"
        if isinstance(item, dict):
            result: dict[str, Any] = {}
            entries = list(item.items())
            for key, child in entries[:_MAX_DICT_ITEMS]:
                result[str(key)] = _REDACTED if _is_sensitive_key(key) else walk(child, depth + 1)
            if len(entries) > _MAX_DICT_ITEMS:
                result["_truncated_keys"] = len(entries) - _MAX_DICT_ITEMS
            return result
        if isinstance(item, (list, tuple, set)):
            values = list(item)
            result = [walk(child, depth + 1) for child in values[:_MAX_LIST_ITEMS]]
            if len(values) > _MAX_LIST_ITEMS:
                result.append({"_truncated_items": len(values) - _MAX_LIST_ITEMS})
            return result
        if isinstance(item, bytes):
            item = item.decode("utf-8", errors="replace")
        if isinstance(item, str):
            text = item
            # Tool plugins sometimes return a JSON object as a string. Parse and redact it.
            stripped = text.strip()
            if stripped and stripped[0] in "[{" and stripped[-1] in "]}":
                try:
                    parsed = json.loads(stripped)
                except (TypeError, ValueError):
                    parsed = None
                if isinstance(parsed, (dict, list)):
                    return walk(parsed, depth + 1)
            if len(text) > _MAX_STRING:
                return text[:_MAX_STRING] + f"\n<TRUNCATED {len(text) - _MAX_STRING} chars>"
            return text
        if item is None or isinstance(item, (bool, int, float)):
            return item
        return str(item)

    return walk(value, 0)
