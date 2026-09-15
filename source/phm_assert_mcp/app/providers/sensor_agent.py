"""Read-only Sensor Agent HTTP boundary, with bounded shared request resources."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import re
import time
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from app.providers.cache import AsyncMemo

PREFIX = "/api/sensor-agent"
logger = logging.getLogger(__name__)


class SensorError(Exception):
    def __init__(self, code: str, message: str, *, path: str = "", http_status: int | None = None):
        super().__init__(message)
        self.code, self.message = code, message
        self.path, self.http_status = path, http_status

    def payload(self) -> dict:
        return {"success": False, "status": self.code, "message": self.message,
                "error": {"code": self.code, "public_message": self.message,
                          "operator_message": self.message, "component": "sensor-agent",
                          "endpoint": self.path, "http_status": self.http_status,
                          "retryable": self.code in {"SENSOR_AGENT_UNAVAILABLE", "SENSOR_AGENT_TIMEOUT", "SENSOR_AGENT_NOT_READY"}}}


def point_not_found(body: Any) -> bool:
    """Require a point-specific negative, never interpret a generic route 404."""
    if not isinstance(body, dict):
        return False
    parts = [body.get(k) for k in ("code", "error_code", "status", "message", "detail")]
    if isinstance(body.get("error"), dict):
        parts.extend(body["error"].get(k) for k in ("code", "message"))
    codes = {"POINT_NOT_FOUND", "SENSOR_NOT_FOUND", "POINT_NOT_MONITORED", "SENSOR_NOT_MONITORED"}
    for part in parts:
        if not isinstance(part, str):
            continue
        if part.upper() in codes or re.search(r"(?i)(?:point|sensor).{0,80}(?:not found|not monitored|not configured|does not exist)", part):
            return True
        if re.search(r"(?:测点|传感器).{0,60}(?:不存在|未配置|未监测|未纳入监测)|未找到(?:测点|传感器)", part):
            return True
    return False


class SensorAgentClient:
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self._client: httpx.AsyncClient | None = None
        self.transport = transport
        self.semaphore = asyncio.Semaphore(settings.sensor_agent_max_parallel)
        self.cache = AsyncMemo(maxsize=2048, ttl=settings.sensor_agent_cache_ttl_seconds)
        self.rules_cache = AsyncMemo(maxsize=4, ttl=settings.sensor_agent_rules_cache_ttl_seconds)

    def client(self) -> httpx.AsyncClient:
        if not self.settings.sensor_agent_enabled or not self.settings.sensor_agent_base_url:
            raise SensorError("SENSOR_AGENT_NOT_CONFIGURED", "传感器自检查询尚未配置，请联系管理员配置服务地址。")
        if self._client is None:
            base = self.settings.sensor_agent_base_url.rstrip("/")
            u = urlsplit(base)
            if u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password or u.query or u.fragment:
                raise SensorError("SENSOR_AGENT_NOT_CONFIGURED", "传感器服务地址配置不正确。")
            headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
            if self.settings.sensor_agent_auth_value:
                header = self.settings.sensor_agent_auth_header
                value = self.settings.sensor_agent_auth_value
                if not re.fullmatch(r"[A-Za-z0-9-]+", header) or any(c in value for c in "\r\n") or header.lower() in {"host", "content-length", "connection"}:
                    raise SensorError("SENSOR_AGENT_NOT_CONFIGURED", "传感器服务认证配置不正确。")
                headers[header] = value
            self._client = httpx.AsyncClient(base_url=base, headers=headers,
                timeout=self.settings.sensor_agent_timeout_seconds, trust_env=False,
                follow_redirects=False, verify=self.settings.sensor_agent_verify_tls,
                limits=httpx.Limits(max_connections=self.settings.sensor_agent_max_parallel,
                                   max_keepalive_connections=self.settings.sensor_agent_max_parallel),
                transport=self.transport)
        return self._client

    async def close(self):
        await self.cache.close()
        await self.rules_cache.close()
        if self._client is not None:
            await self._client.aclose()

    async def get(self, path: str, params=None, *, refresh=False, rules=False, point=False) -> dict:
        async def fetch():
            return await self._request(path, params or {}, point=point)
        trace = {}
        result = await (self.rules_cache if rules else self.cache).get(
            [path, params or {}, point], fetch, refresh=refresh, trace=trace)
        result["source"]["cache_mode"] = trace["mode"]
        result["source"]["cache_age_seconds"] = max(0, round(
            (datetime.now(timezone.utc) - datetime.fromisoformat(result["source"]["fetched_at"])).total_seconds(), 3))
        result["source"]["refresh_requested"] = bool(refresh)
        return result

    async def _request(self, path, params, *, point=False):
        started = time.monotonic()
        code = None
        body, data = None, bytearray()
        try:
            for attempt in range(self.settings.sensor_agent_max_retries + 1):
                try:
                    async with self.semaphore:
                        async with asyncio.timeout(self.settings.sensor_agent_timeout_seconds):
                            async with self.client().stream("GET", path, params=params) as response:
                                code = response.status_code
                                data = bytearray()
                                async for chunk in response.aiter_bytes():
                                    data.extend(chunk)
                                    if len(data) > self.settings.sensor_agent_max_response_mb * 1024 * 1024:
                                        raise SensorError("SENSOR_AGENT_BAD_RESPONSE", "传感器接口响应超过读取上限，无法确认完整结果。", path=path, http_status=code)
                                try:
                                    body = json.loads(data)
                                except (ValueError, UnicodeError):
                                    body = None
                    if code in {502, 503, 504} and attempt < self.settings.sensor_agent_max_retries:
                        await asyncio.sleep(0.15 * (attempt + 1))
                        continue
                    if point and code in {200, 404} and point_not_found(body):
                        raise SensorError("POINT_NOT_MONITORED", "该测点未纳入当前传感器自检服务的监测范围。", path=path, http_status=code)
                    if code in {401, 403}:
                        raise SensorError("SENSOR_AGENT_ACCESS_DENIED", "传感器查询权限不足，无法确认监测或故障状态。", path=path, http_status=code)
                    if not 200 <= code < 300:
                        raise SensorError("SENSOR_AGENT_NOT_READY" if path == "/ready" and code == 503 else "SENSOR_AGENT_UNAVAILABLE", "传感器自检服务暂时无法完成查询，不能据此判断是否存在故障或是否在线。", path=path, http_status=code)
                    if not isinstance(body, dict) or body.get("success") is not True:
                        raise SensorError("SENSOR_AGENT_BAD_RESPONSE", "传感器接口未返回有效的成功结果，无法确认状态。", path=path, http_status=code)
                    if path == "/ready":
                        if body.get("status") != "READY":
                            raise SensorError("SENSOR_AGENT_NOT_READY", "传感器自检服务尚未就绪，无法确认当前状态。", path=path, http_status=code)
                    elif "data" not in body:
                        raise SensorError("SENSOR_AGENT_BAD_RESPONSE", "传感器接口缺少数据内容。", path=path, http_status=code)
                    return {"body": body, "source": {"endpoint": path, "fetched_at": datetime.now(timezone.utc).isoformat(),
                            "http_status": code, "elapsed_ms": round((time.monotonic()-started)*1000, 2),
                            "requested_limit": params.get("limit"), "response_bytes": len(data)}}
                except (httpx.TimeoutException, TimeoutError) as exc:
                    if attempt < self.settings.sensor_agent_max_retries:
                        continue
                    raise SensorError("SENSOR_AGENT_TIMEOUT", "传感器自检服务响应超时，无法确认当前状态。", path=path, http_status=code) from exc
                except httpx.RequestError as exc:
                    if attempt < self.settings.sensor_agent_max_retries:
                        continue
                    raise SensorError("SENSOR_AGENT_UNAVAILABLE", "无法连接传感器自检服务，无法确认当前状态。", path=path) from exc
        finally:
            payload_data = body.get("data") if isinstance(body, dict) else None
            records = payload_data.get("records") if isinstance(payload_data, dict) else None
            logger.info("sensor_http_completed", extra={"fields": {"endpoint_kind": path.rsplit("/", 1)[-1],
                        "http_status": code, "elapsed_ms": round((time.monotonic()-started)*1000, 2),
                        "requested_limit": params.get("limit"), "response_bytes": len(data),
                        "returned_records": len(records) if isinstance(records, list) else None}})

    async def ready(self, *, refresh=False):
        return await self.get("/ready", refresh=refresh)

    async def faults(self, scope, *, refresh=False):
        return await self.get(PREFIX+"/faults", {"only_active": "true" if scope == "active" else "false", "limit": self.settings.sensor_agent_fetch_limit}, refresh=refresh)

    async def dashboard(self, *, refresh=False):
        return await self.get(PREFIX+"/dashboard/sensors", {"include_simulation": "false"}, refresh=refresh)

    async def rules(self, *, refresh=False):
        return await self.get(PREFIX+"/admin/rules", refresh=refresh, rules=True)

    async def monitored_points(self, *, refresh=False):
        # The documented upstream has neither filters nor pagination.
        return await self.get(PREFIX+"/admin/points", {"limit": self.settings.sensor_agent_fetch_limit}, refresh=refresh)

    async def point_state(self, equip_num, point_num, *, refresh=False):
        return await self.get(PREFIX+"/admin/points/"+quote(point_num, safe="")+"/state", {"equip_num": equip_num}, refresh=refresh, point=True)

    async def evidence(self, fault_id, *, refresh=False):
        return await self.get(PREFIX+"/internal/faults/"+quote(fault_id, safe="")+"/evidence", refresh=refresh)
