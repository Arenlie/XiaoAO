from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.api.dependencies import RequestIdentity, get_container, require_controller_admin
from app.container import AppContainer
from app.schemas.common import ApiResponse

router = APIRouter(prefix="/controller/tools", tags=["controller-tools"])


@router.get("", response_model=ApiResponse[list[dict[str, Any]]])
async def list_tools(
    request: Request,
    identity: RequestIdentity = Depends(require_controller_admin),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[list[dict[str, Any]]]:
    del identity
    return ApiResponse(
        data=[item.model_dump(mode="json") for item in container.tool_registry.list_descriptors()],
        request_id=request.state.request_id,
    )
