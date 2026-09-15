from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from app.api.dependencies import RequestIdentity, get_container, require_controller_admin
from app.container import AppContainer
from app.domain.exceptions import AppError
from app.schemas.common import ApiResponse

router = APIRouter(prefix="/controller/workflows", tags=["controller-workflows"])


@router.get("", response_model=ApiResponse[list[dict[str, Any]]])
async def list_workflows(
    request: Request,
    identity: RequestIdentity = Depends(require_controller_admin),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[list[dict[str, Any]]]:
    del identity
    return ApiResponse(
        data=container.workflow_registry.describe(container.tool_registry),
        request_id=request.state.request_id,
    )


@router.get("/{workflow_id}", response_model=ApiResponse[dict[str, Any]])
async def get_workflow(
    workflow_id: str,
    request: Request,
    identity: RequestIdentity = Depends(require_controller_admin),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict[str, Any]]:
    del identity
    rows = container.workflow_registry.describe(container.tool_registry)
    item = next((row for row in rows if row.get("workflow_id") == workflow_id), None)
    if item is None:
        raise AppError("WORKFLOW_NOT_FOUND", f"业务链路未注册: {workflow_id}", 404)
    return ApiResponse(data=item, request_id=request.state.request_id)
