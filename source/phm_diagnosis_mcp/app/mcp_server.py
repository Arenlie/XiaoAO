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

runtime = Runtime.create()
mcp = MCPServer("phm-diagnosis-mcp")


@mcp.tool()
async def analyze_chart(
    chart_type: str,
    waveform: dict[str, Any] | None = None,
    trend: dict[str, Any] | None = None,
    speed_rpm: float | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """分析一个指定图谱。波形图传waveform，趋势图传trend；阶比类图谱需要speed_rpm。"""
    args = locals().copy()
    return await runtime.executor.run(
        "analyze_chart",
        args,
        lambda: asyncio.to_thread(runtime.chart.analyze, chart_type, waveform, trend, speed_rpm, options),
    )


@mcp.tool()
async def diagnose_point(
    point_no: str,
    alarm: dict[str, Any] | None = None,
    waveform: dict[str, Any] | None = None,
    feature_trends: list[dict[str, Any]] | None = None,
    temperature_trends: list[dict[str, Any]] | None = None,
    speed_rpm: float | None = None,
    use_model: bool = True,
) -> dict[str, Any]:
    """对设备单个测点进行综合诊断。先判断是否异常；没有异常证据时不会强制输出故障。"""
    args = locals().copy()
    return await runtime.executor.run(
        "diagnose_point",
        args,
        lambda: runtime.diagnosis.diagnose_point(
            point_no,
            alarm,
            waveform,
            feature_trends,
            temperature_trends,
            speed_rpm,
            use_model,
        ),
    )


@mcp.tool()
async def diagnose_device(
    device_code: str,
    points: list[dict[str, Any]],
    speed_rpm: float | None = None,
    use_model: bool = True,
) -> dict[str, Any]:
    """对设备全部测点做综合诊断。points中每个元素包含point_no及该测点已有的waveform/feature_trends/temperature_trends。"""
    args = locals().copy()
    return await runtime.executor.run(
        "diagnose_device",
        args,
        lambda: runtime.diagnosis.diagnose_device(device_code, points, speed_rpm, use_model),
    )


@mcp.tool()
async def get_model_admission() -> dict[str, object]:
    """返回综合诊断模型当前是否启用、健康以及是否允许调用。"""
    return await runtime.executor.run("get_model_admission", {}, runtime.admission.status)


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": runtime.settings.app_name, "version": runtime.settings.app_version})


async def ready(_: Request) -> JSONResponse:
    audit_ok = True
    if runtime.audit:
        try:
            audit_ok = await asyncio.to_thread(runtime.audit.ping)
        except Exception:
            audit_ok = False
    code = 200 if audit_ok else 503
    return JSONResponse({"status": "ready" if code == 200 else "not_ready", "audit": audit_ok}, status_code=code)


security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
mcp_app = mcp.streamable_http_app(
    stateless_http=True,
    json_response=True,
    max_request_body_size=runtime.settings.mcp_max_request_body_bytes,
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
