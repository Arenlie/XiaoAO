from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request

from app.agents.contracts import AgentHealthResult, AgentHealthStatus
from app.api.dependencies import RequestIdentity, get_container, require_controller_admin
from app.container import AppContainer
from app.domain.exceptions import AppError
from app.schemas.agent import AgentRuntimeUpdate
from app.schemas.common import ApiResponse

router = APIRouter(prefix="/controller/agents", tags=["controller-agents"])


def _view(container: AppContainer, descriptor) -> dict[str, Any]:
    payload = descriptor.model_dump(mode="json")
    payload["adapter_registered"] = (
        descriptor.agent_id in container.agent_adapter_registry.registered_ids()
    )
    return payload


@router.get("", response_model=ApiResponse[list[dict[str, Any]]])
async def list_agents(
    request: Request,
    identity: RequestIdentity = Depends(require_controller_admin),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[list[dict[str, Any]]]:
    del identity
    rows = await container.agent_registry_service.list_descriptors(use_cache=False)
    return ApiResponse(
        data=[_view(container, item) for item in rows],
        request_id=request.state.request_id,
    )


@router.get("/{agent_id}", response_model=ApiResponse[dict[str, Any]])
async def get_agent(
    agent_id: str,
    request: Request,
    identity: RequestIdentity = Depends(require_controller_admin),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict[str, Any]]:
    del identity
    item = await container.agent_registry_service.get_descriptor(agent_id)
    return ApiResponse(data=_view(container, item), request_id=request.state.request_id)


@router.patch("/{agent_id}", response_model=ApiResponse[dict[str, Any]])
async def update_agent(
    agent_id: str,
    payload: AgentRuntimeUpdate,
    request: Request,
    identity: RequestIdentity = Depends(require_controller_admin),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict[str, Any]]:
    item = await container.agent_registry_service.update(
        agent_id,
        payload,
        updated_by=identity.user_token,
    )
    return ApiResponse(data=_view(container, item), request_id=request.state.request_id)


@router.post("/{agent_id}/enable", response_model=ApiResponse[dict[str, Any]])
async def enable_agent(
    agent_id: str,
    request: Request,
    identity: RequestIdentity = Depends(require_controller_admin),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict[str, Any]]:
    item = await container.agent_registry_service.set_enabled(
        agent_id,
        enabled=True,
        updated_by=identity.user_token,
    )
    return ApiResponse(data=_view(container, item), request_id=request.state.request_id)


@router.post("/{agent_id}/disable", response_model=ApiResponse[dict[str, Any]])
async def disable_agent(
    agent_id: str,
    request: Request,
    identity: RequestIdentity = Depends(require_controller_admin),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict[str, Any]]:
    descriptor = await container.agent_registry_service.get_descriptor(agent_id)
    if descriptor.mandatory:
        raise AppError("MANDATORY_AGENT_CANNOT_BE_DISABLED", "基础补充信息智能体不能关闭", 409)
    item = await container.agent_registry_service.set_enabled(
        agent_id,
        enabled=False,
        updated_by=identity.user_token,
    )
    return ApiResponse(data=_view(container, item), request_id=request.state.request_id)


@router.post("/{agent_id}/health-check", response_model=ApiResponse[dict[str, Any]])
async def health_check_agent(
    agent_id: str,
    request: Request,
    identity: RequestIdentity = Depends(require_controller_admin),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict[str, Any]]:
    descriptor = await container.agent_registry_service.get_descriptor(agent_id)
    try:
        adapter = container.agent_adapter_registry.get(agent_id)
    except AppError:
        result = AgentHealthResult(
            agent_id=agent_id,
            status=(
                AgentHealthStatus.DISABLED
                if not descriptor.enabled
                else AgentHealthStatus.UNHEALTHY
            ),
            message=(
                descriptor.maintenance_message
                or "智能体适配器尚未注册；该能力当前仅保留接入位。"
            ),
            checked_at=datetime.now(UTC).isoformat(),
            details={"adapter_registered": False},
        )
    else:
        result = await adapter.health_check()
    saved = await container.agent_registry_service.save_health(
        result,
        updated_by=identity.user_token,
    )
    return ApiResponse(
        data={
            "health": result.model_dump(mode="json"),
            "agent": _view(container, saved),
        },
        request_id=request.state.request_id,
    )

