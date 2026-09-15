from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api.dependencies import get_container
from app.container import AppContainer
from app.domain.exceptions import AppError

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/ready")
async def ready(container: AppContainer = Depends(get_container)) -> dict:
    await container.database.ping()
    await container.redis_manager.ping()
    payload = {"status": "ready", "postgresql": "ok", "redis": "ok"}
    if container.settings.phm_asset_mcp_enabled:
        try:
            asset_ready = await container.phm_asset_mcp_client.ready(
                container.settings.phm_asset_mcp_ready_url
            )
        except Exception as exc:
            raise AppError(
                "PHM_ASSET_MCP_NOT_READY",
                f"PHM Asset MCP 未就绪: {exc}",
                503,
            ) from exc
        payload["phm_asset_mcp"] = str(
            asset_ready.get("status") or ("ready" if asset_ready.get("core_ready") else "unknown")
        )
    if container.settings.phm_data_mcp_enabled:
        try:
            mcp_ready = await container.phm_data_mcp_client.ready(
                container.settings.phm_data_mcp_ready_url
            )
        except Exception as exc:
            raise AppError(
                "PHM_DATA_MCP_NOT_READY",
                f"PHM Data MCP 未就绪: {exc}",
                503,
            ) from exc
        payload["phm_data_mcp"] = str(mcp_ready.get("status") or "ready")
    if container.settings.phm_diagnosis_mcp_enabled:
        try:
            diagnosis_ready = await container.phm_diagnosis_mcp_client.ready(
                container.settings.phm_diagnosis_mcp_ready_url
            )
        except Exception as exc:
            raise AppError(
                "PHM_DIAGNOSIS_MCP_NOT_READY",
                f"PHM Diagnosis MCP 未就绪: {exc}",
                503,
            ) from exc
        payload["phm_diagnosis_mcp"] = str(
            diagnosis_ready.get("status") or "ready"
        )
    if container.settings.phm_feature_mcp_enabled:
        try:
            feature_tools = await container.phm_feature_mcp_client.verify_expected_tools()
        except Exception as exc:
            raise AppError(
                "PHM_FEATURE_MCP_NOT_READY",
                f"PHM Feature MCP 未就绪: {exc}",
                503,
            ) from exc
        payload["phm_feature_mcp"] = "ready"
        payload["phm_feature_mcp_tools"] = len(feature_tools)
    return payload


@router.get("/metrics")
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
