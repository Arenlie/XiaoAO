from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from app.repositories.audit import AuditRepository
from app.performance import attach_trace, performance_request
from app.config import Settings


class ToolExecutor:
    def __init__(self, audit: AuditRepository, settings: Settings):
        self.audit = audit
        self.settings = settings

    async def run(self, tool_name: str, input_data: dict[str, Any], func: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        started = time.perf_counter()
        success = False
        error: str | None = None
        audit_extra: dict[str, Any] = {}
        result: dict[str, Any] = {"success": False}
        with performance_request(self.settings, "phm-data-mcp", tool_name) as perf:
            try:
                result = await asyncio.to_thread(func)
                success = bool(result.get("success", True))
                audit_extra = dict(result.pop("_audit", {}) or {})
                return attach_trace(result, perf)
            except Exception as exc:
                error = str(exc)
                result = {"success": False, "error": error}
                return attach_trace(result, perf)
            finally:
                duration_ms = int((time.perf_counter() - started) * 1000)
                summary = {"success": success, **audit_extra}
                await asyncio.to_thread(self.audit.record, tool_name, duration_ms, success, input_data, summary, error)
