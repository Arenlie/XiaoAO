from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from app.runtime import Runtime
from app.health_output import format_health_output

runtime = Runtime.create()
mcp = MCPServer("phm-data-mcp")


@mcp.tool()
async def query_readonly_snapshot(rows: list[dict], sql: str, complete: bool, limit: int = 1000) -> dict:
    """复核并隔离执行本轮完整数据上的高级统计，不连接生产数据库执行模型SQL。"""
    from app.services.readonly_snapshot import query
    return await runtime.executor.run("query_readonly_snapshot", {"row_count":len(rows),"sql":sql,"complete":complete},
        lambda: query(rows,sql,complete=complete,limit=limit))


@mcp.tool()
async def query_alarm_collection(equip_nos: list[str], operation: str = "count", metric: str = "count_records",
        alarm_types: list[str] | None = None, alarm_state: str = "active", start_time: str | None = None,
        end_time: str | None = None, comparison_start: str | None = None, comparison_end: str | None = None,
        order: str = "desc", limit: int = 50000) -> dict:
    """对已确认的设备集合进行统一报警数量、排名与期间比较；不会以名称猜测设备。"""
    from app.alarm.collection import query_collection
    args = dict(equip_nos=equip_nos, operation=operation, metric=metric, alarm_types=alarm_types,
        alarm_state=alarm_state, start_time=start_time, end_time=end_time, comparison_start=comparison_start,
        comparison_end=comparison_end, order=order, limit=limit)
    return await runtime.executor.run("query_alarm_collection", args, lambda: query_collection(runtime.mysql, **args))


@mcp.tool()
async def query_health_collection(scope_type: str, scope_ids: list[str], operation: str = "rank", order: str = "asc", limit: int = 10) -> dict[str, Any]:
    """对全部指定区域或设备读取最新平台健康度，确定性排序/汇总，明确缺失和并列；不调用诊断。"""
    from app.services.health_collection import query_collection
    args = locals().copy()
    args.pop("query_collection", None)
    return await runtime.executor.run("query_health_collection", args,
        lambda: query_collection(runtime.health, scope_type, scope_ids, operation=operation, order=order, limit=limit))


@mcp.tool()
async def get_waveform(
    device_code: str,
    point_no: str,
    target_time: str | None = None,
    mode: str = "latest",
    search_window_seconds: int = 86400,
) -> dict[str, Any]:
    """读取振动波形。未给时间时默认最新；返回值为float32小端数组的base64，并附带解码方式。"""
    args = locals().copy()
    return await runtime.executor.run(
        "get_waveform",
        args,
        lambda: runtime.data.get_waveform(device_code, point_no, target_time, mode, search_window_seconds),
    )


@mcp.tool()
async def get_feature_trend(
    device_code: str,
    point_id: str,
    start_time: str | None = None,
    end_time: str | None = None,
    days: int = 30,
    kpi_ids: list[str] | None = None,
    return_mode: str = "auto",
) -> dict[str, Any]:
    """读取特征趋势。默认查询最近30天；短数据直接JSON，长数据自动使用gzip JSON + base64。"""
    args = locals().copy()
    return await runtime.executor.run(
        "get_feature_trend",
        args,
        lambda: runtime.data.get_feature_trend(device_code, point_id, start_time, end_time, days, kpi_ids, return_mode),
    )


@mcp.tool()
async def get_temperature_trend(
    device_code: str,
    point_id: str,
    start_time: str | None = None,
    end_time: str | None = None,
    days: int = 30,
    kpi_id: str | None = None,
    return_mode: str = "auto",
) -> dict[str, Any]:
    """读取温度趋势。未指定kpi_id时按测点的000温度KPI查询。"""
    args = locals().copy()
    return await runtime.executor.run(
        "get_temperature_trend",
        args,
        lambda: runtime.data.get_temperature_trend(device_code, point_id, start_time, end_time, days, kpi_id, return_mode),
    )


@mcp.tool()
async def check_data_availability(
    device_code: str,
    wave_point_no: str | None = None,
    feature_point_id: str | None = None,
) -> dict[str, Any]:
    """快速检查指定设备/测点是否存在波形和特征数据，并返回可见KPI。"""
    args = locals().copy()
    return await runtime.executor.run(
        "check_data_availability",
        args,
        lambda: runtime.data.check_data_availability(device_code, wave_point_no, feature_point_id),
    )


@mcp.tool()
async def get_device_data(
    device_code: str,
    target_time: str | None = None,
    trend_days: int = 30,
    search_window_seconds: int = 86400,
    trend_hours: float | None = None,
    fallback_trend_hours: float | None = None,
) -> dict[str, Any]:
    """一次获取设备全部可用测点数据；trend_hours优先，超时可按fallback_trend_hours降级，未给时间时波形取最新。"""
    args = locals().copy()
    return await runtime.executor.run(
        "get_device_data",
        args,
        lambda: runtime.data.get_device_data(
            device_code,
            target_time,
            trend_days,
            search_window_seconds,
            trend_hours,
            fallback_trend_hours,
        ),
    )


@mcp.tool()
async def get_data_snapshot(
    device_code: str,
    wave_point_no: str | None = None,
    feature_point_id: str | None = None,
    target_time: str | None = None,
    kpi_ids: list[str] | None = None,
    trend_days: int = 30,
    tolerance_seconds: int = 3600,
    search_window_seconds: int = 86400,
    trend_hours: float | None = None,
) -> dict[str, Any]:
    """一次获取波形和特征趋势。尽量围绕同一时间锚点取最近数据；偏差超限会明确返回warning。"""
    args = locals().copy()
    return await runtime.executor.run(
        "get_data_snapshot",
        args,
        lambda: runtime.data.get_data_snapshot(
            device_code,
            wave_point_no,
            feature_point_id,
            target_time,
            kpi_ids,
            trend_days,
            tolerance_seconds,
            search_window_seconds,
            trend_hours,
        ),
    )


@mcp.tool()
async def query_alarm_records(
    query: str = "",
    alarm_types: list[str] | None = None,
    equip_no: str | None = None,
    equip_name: str | None = None,
    equip_name_keyword: str | None = None,
    point_no: str | None = None,
    model_no: str | None = None,
    space_link: str | None = None,
    space_name: str | None = None,
    time_mode: str = "default",
    target_time: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    metric: str | None = None,
    group_by: str | None = None,
    limit: int = 20,
    alarm_state: str | None = None,
    end_exclusive: bool = False,
) -> dict[str, Any]:
    """查询阈值、趋势、机理/诊断和AI报警。

    query 可用于报警类型、状态、统计等业务语义；时间范围必须通过结构化
    time_mode/target_time/start_time/end_time 传入。Data MCP 不解析“最近一周、
    昨天、本月”等中文自然语言时间，避免与上游运行时钟产生双重时间语义。
    默认无结构化时间条件时查询当前未结束报警。
    """
    args = locals().copy()
    return await runtime.executor.run(
        "query_alarm_records",
        args,
        lambda: runtime.alarm.query(**args),
    )


@mcp.tool()
async def query_health_score(
    scope_type: str,
    scope_id: str,
    start_time: str | None = None,
    end_time: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """查询健康度。scope_type=device查询设备健康度，默认最新，传时间范围时查询历史；scope_type=space按space_id查询Redis实时区域健康度。"""
    args = locals().copy()
    result = await runtime.executor.run(
        "query_health_score",
        args,
        lambda: runtime.health.query(scope_type, scope_id, start_time, end_time, limit),
    )
    return format_health_output(result)


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": runtime.settings.app_name, "version": runtime.settings.app_version})


async def ready(_: Request) -> JSONResponse:
    def check() -> dict[str, bool]:
        result: dict[str, bool] = {}
        try:
            result["mongo"] = runtime.mongo.ping()
        except Exception:
            result["mongo"] = False
        try:
            result["mysql"] = runtime.mysql.ping()
        except Exception:
            result["mysql"] = False
        try:
            result["health_mongo"] = runtime.health_repository.ping_mongo()
        except Exception:
            result["health_mongo"] = False
        try:
            result["health_redis"] = runtime.health_repository.ping_redis()
        except Exception:
            result["health_redis"] = False
        if runtime.settings.audit_enabled:
            result["audit"] = runtime.audit.ping()
        return result

    checks = await asyncio.to_thread(check)
    code = 200 if all(checks.values()) else 503
    return JSONResponse({"status": "ready" if code == 200 else "not_ready", **checks}, status_code=code)


# This MCP is an internal service; DNS-rebinding checks are expected to be enforced by the internal gateway/reverse proxy.
security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
mcp_app = mcp.streamable_http_app(
    json_response=True,
    transport_security=security,
)


@asynccontextmanager
async def lifespan(_: Starlette) -> AsyncIterator[None]:
    runtime.start()
    async with mcp.session_manager.run():
        try:
            yield
        finally:
            runtime.close()


app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Route("/ready", ready, methods=["GET"]),
        Mount("/", app=mcp_app),
    ],
    lifespan=lifespan,
)
