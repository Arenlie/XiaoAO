from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Optional

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount

from app.config import Settings, get_settings
from app.core.signal_codec import decode_signal
from app.core.vibration_features import FEATURE_SET_CATALOG
from app.engine import SpeedJob
from app.schemas import (
    AlgorithmConfig,
    CapabilityResponse,
    DeviceInfo,
    FeatureOptions,
    FeatureResponse,
    RotationalSpeedFeatureResponse,
    SignalPayload,
    VibrationSignalInput,
)
from app.services.analysis_service import AnalysisService
from app.performance import attach_trace, instrument_object, performance_request, current_recorder

logger = logging.getLogger(__name__)
settings = get_settings()


@dataclass
class AppContext:
    settings: Settings
    analysis: AnalysisService


@asynccontextmanager
async def app_lifespan(server: MCPServer) -> AsyncIterator[AppContext]:
    service = AnalysisService(settings)
    instrument_object(service.speed_engine, component_code="speed_engine", component_cn="转速识别算法", category="algorithm")
    logger.info(
        "mcp_service_started service=%s version=%s algorithm=%s workers=%s concurrency=%s",
        settings.service_name,
        settings.service_version,
        settings.algorithm_version,
        settings.max_workers,
        settings.max_concurrent_tasks,
    )
    try:
        yield AppContext(settings=settings, analysis=service)
    finally:
        await service.close()
        logger.info("mcp_service_stopped service=%s", settings.service_name)


mcp = MCPServer(
    name=settings.service_name,
    title="PHM Feature MCP",
    description=(
        "Industrial MCP server dedicated to extracting structured scalar features from vibration waveforms. "
        "Signal preprocessing, FFT, PSD and Hilbert-envelope calculations are internal implementation details "
        "and are not exposed as standalone tools."
    ),
    instructions=(
        "Use extract_vibration_features to convert a vibration waveform into structured time-domain, "
        "frequency-domain, envelope-domain and optional band-energy features. "
        "Use extract_rotational_speed_feature only when shaft rotational frequency, 1X or RPM is required. "
        "For large waveforms prefer float32_base64 over JSON number arrays."
    ),
    version=settings.service_version,
    lifespan=app_lifespan,
    log_level=settings.log_level,
)


def _analysis(ctx: Context[AppContext]) -> AnalysisService:
    return ctx.request_context.lifespan_context.analysis


@mcp.tool()
async def extract_vibration_features(
    ctx: Context[AppContext],
    signal_input: VibrationSignalInput,
    options: Optional[FeatureOptions] = None,
) -> dict[str, Any]:
    """
    Extract structured scalar vibration features from one waveform.

    Feature sets:
    - basic: compact time-domain feature set;
    - standard: full time-domain + common frequency-domain features;
    - bearing: standard + Hilbert-envelope scalar features;
    - full: bearing set + any user-defined frequency-band energy features.

    This tool does not return processed waveforms, FFT curves or PSD arrays. Required
    detrending, PSD, filtering and Hilbert calculations remain internal to feature extraction.
    """
    opts = options or FeatureOptions()
    with performance_request(settings, "phm-feature-mcp", "extract_vibration_features") as perf:
        await ctx.report_progress(10, total=100, message="振动波形解码")
        with perf.span("feature.decode", "振动波形解码", category="decode", description="将输入波形转换为数值数组"):
            x = decode_signal(signal_input.data)
        await ctx.report_progress(30, total=100, message=f"开始提取 {opts.feature_set} 振动特征集")
        with perf.span("feature.algorithm", "振动特征算法计算", category="algorithm", description="执行时域、频域、包络等确定性算法"):
            result = await _analysis(ctx).extract_features(signal_input, x, opts)
        await ctx.report_progress(90, total=100, message=f"已提取 {result.feature_count} 个标量特征")
        await ctx.report_progress(100, total=100, message="振动特征提取完成")
        return attach_trace(result, perf)


@mcp.tool()
async def extract_rotational_speed_feature(
    ctx: Context[AppContext],
    fs_hz: float,
    acceleration: Optional[SignalPayload] = None,
    velocity: Optional[SignalPayload] = None,
    device_info: Optional[DeviceInfo] = None,
    algorithm_config: Optional[AlgorithmConfig] = None,
    use_llm_judge: Optional[bool] = None,
    request_id: Optional[str] = None,
) -> dict[str, Any]:
    """
    Extract shaft rotational-frequency and RPM features from vibration waveforms.

    At least one of acceleration or velocity must be provided. If only acceleration
    is supplied, the validated internal algorithm can derive velocity for the
    speed/acceleration dual-spectrum rule logic. Optional LLM arbitration is limited
    to conflicts between algorithm-generated candidates and does not invent a new RPM.
    """
    if fs_hz <= 0:
        raise ValueError("fs_hz must be > 0")
    if acceleration is None and velocity is None:
        raise ValueError("at least one of acceleration or velocity must be provided")

    with performance_request(settings, "phm-feature-mcp", "extract_rotational_speed_feature") as perf:
        await ctx.report_progress(5, total=100, message="转速特征提取请求已接收")
        with perf.span("rpm.decode", "转速分析波形解码", category="decode", description="解码加速度/速度波形"):
            acc_arr = decode_signal(acceleration) if acceleration is not None else None
            vel_arr = decode_signal(velocity) if velocity is not None else None
        await ctx.report_progress(15, total=100, message="振动波形解码完成")
        job = SpeedJob(
            request_id=request_id, fs_hz=fs_hz, acceleration=acc_arr, velocity=vel_arr,
            device_info=device_info, algorithm_config=algorithm_config or AlgorithmConfig(),
            use_llm_judge=use_llm_judge,
        )
        await ctx.report_progress(30, total=100, message="开始提取旋转频率候选特征")
        with perf.span("rpm.algorithm", "转速候选识别与融合", category="algorithm", description="执行速度谱/加速度谱候选识别、融合与可选大模型仲裁"):
            result = await _analysis(ctx).extract_rotational_speed_feature(job)
        await ctx.report_progress(90, total=100, message="转速候选与冲突处理完成")
        if result.reason:
            await ctx.report_progress(95, total=100, message=f"判断依据：{result.reason[:300]}")
        await ctx.report_progress(100, total=100, message="转速特征提取完成")
        return attach_trace(result, perf)


@mcp.resource("phm-feature://capabilities")
def capabilities() -> str:
    """Machine-readable feature catalog and runtime limits of this MCP server."""
    payload = CapabilityResponse(
        service=settings.service_name,
        version=settings.service_version,
        algorithm_version=settings.algorithm_version,
        transport="streamable-http",
        endpoint=settings.mcp_path,
        tools=[
            "extract_vibration_features",
            "extract_rotational_speed_feature",
        ],
        feature_sets=FEATURE_SET_CATALOG,
        input_encodings=["samples", "float32_base64(little-endian float32)"],
        limits={
            "max_samples_per_request": settings.max_samples_per_request,
            "max_request_body_size": settings.max_request_body_size,
        },
    )
    return json.dumps(payload.model_dump(), ensure_ascii=False)


# Deployment requirement: no authentication, CORS fully open. Network exposure
# should be restricted by the surrounding internal network/firewall if required.
_transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
_mcp_asgi = mcp.streamable_http_app(
    host=settings.host,
    streamable_http_path=settings.mcp_path,
    json_response=False,
    max_request_body_size=settings.max_request_body_size,
    transport_security=_transport_security,
)

_cors_mcp_asgi = CORSMiddleware(
    app=_mcp_asgi,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["MCP-Protocol-Version", "Mcp-Method", "Mcp-Name"],
)


@asynccontextmanager
async def _http_lifespan(app: Starlette):
    # streamable_http_app() is mounted below, so the outer ASGI lifespan must
    # explicitly run the MCP session manager. The MCPServer lifespan is entered
    # once by this manager and provides AppContext to all requests.
    async with mcp.session_manager.run():
        yield


app = Starlette(
    routes=[Mount("/", app=_cors_mcp_asgi)],
    lifespan=_http_lifespan,
)
