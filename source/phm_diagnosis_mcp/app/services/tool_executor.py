from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from app.repositories.audit import AuditRepository
from app.performance import attach_trace, performance_request
from app.config import Settings


def _summary(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"values_base64", "payload_base64"}:
                out[key] = f"<base64:{len(str(item))} chars>"
            else:
                out[key] = _summary(item)
        return out
    if isinstance(value, list):
        if len(value) > 20:
            return [_summary(x) for x in value[:20]] + [f"... {len(value) - 20} more"]
        return [_summary(x) for x in value]
    return value


class ToolExecutor:
    def __init__(self, audit: AuditRepository | None, settings: Settings):
        self.audit = audit
        self.settings = settings

    async def run(self, tool_name: str, args: dict[str, Any], func: Callable[[], Awaitable[dict[str, Any]] | dict[str, Any]]) -> dict[str, Any]:
        started = datetime.now(timezone.utc)
        begin = time.perf_counter()
        result: dict[str, Any] = {"success": False}
        success = False
        error: str | None = None
        with performance_request(self.settings, "phm-diagnosis-mcp", tool_name) as perf:
            try:
                value = func()
                if asyncio.iscoroutine(value):
                    value = await value
                result = value
                success = bool(result.get("success", True))
                error = None if success else str(result.get("error") or "tool failed")
                return attach_trace(result, perf)
            except Exception as exc:
                success = False
                error = str(exc)
                result = {"success": False, "error": error}
                return attach_trace(result, perf)
            finally:
                if self.audit:
                    duration_ms = int((time.perf_counter() - begin) * 1000)
                    try:
                        await asyncio.to_thread(
                            self.audit.log_tool, tool_name, started, duration_ms, success,
                            _summary(args), _summary(result) if isinstance(result, dict) else None, error,
                        )
                    except Exception:
                        pass
