from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.dependencies import RequestIdentity, get_container, require_user
from app.container import AppContainer
from app.schemas.common import ApiResponse
from app.schemas.profile import UpdateProfileRequest, UserProfileView

router = APIRouter(tags=["profile"])


@router.get("/profile", response_model=ApiResponse[UserProfileView])
async def get_profile(
    request: Request,
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[UserProfileView]:
    profile = await container.profile_service.get(identity.user_token)
    return ApiResponse(
        data=UserProfileView(profile_json=profile), request_id=request.state.request_id
    )


@router.patch("/profile", response_model=ApiResponse[UserProfileView])
async def update_profile(
    payload: UpdateProfileRequest,
    request: Request,
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[UserProfileView]:
    profile = await container.profile_service.update(
        identity.user_token, payload.profile_json
    )
    return ApiResponse(
        data=UserProfileView(profile_json=profile), request_id=request.state.request_id
    )


@router.delete("/profile", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> Response:
    await container.profile_service.delete(identity.user_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/suggested-queries", response_model=ApiResponse[list[str]])
async def suggested_queries(
    request: Request,
    app_code: str = Query(default="xiaoao", max_length=64),
    limit: int = Query(default=10, ge=1, le=30),
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[list[str]]:
    rows = await container.query_statistics_service.suggestions(
        identity.user_token, app_code, limit
    )
    return ApiResponse(data=rows, request_id=request.state.request_id)
