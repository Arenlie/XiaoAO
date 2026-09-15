from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request, Query, Response, status

from app.api.dependencies import RequestIdentity, get_container, require_generation_identity, require_user
from app.container import AppContainer
from app.metrics import CHAT_REQUESTS
from app.schemas.common import ApiResponse
from app.schemas.message import (
    EditAndResubmitRequest,
    MessageView,
    ExecutionEventView,
    RegenerateRequest,
    SendMessageAccepted,
)

router = APIRouter(prefix="/messages", tags=["messages"])


@router.post(
    "/{user_message_id}/edit-and-resubmit",
    response_model=ApiResponse[SendMessageAccepted],
    status_code=status.HTTP_202_ACCEPTED,
)
async def edit_and_resubmit(
    user_message_id: UUID,
    payload: EditAndResubmitRequest,
    request: Request,
    identity: RequestIdentity = Depends(require_generation_identity),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[SendMessageAccepted]:
    result = await container.generation_service.edit_and_resubmit(
        message_id=user_message_id,
        content=payload.content.strip(),
        user_token=identity.user_token,
        data_access_token=identity.data_access_token or "",
        request_id=request.state.request_id,
        attachment_ids=payload.attachments,
        execution_mode=payload.execution_mode,
    )
    CHAT_REQUESTS.labels(operation="EDIT", status="accepted").inc()
    return ApiResponse(data=result, request_id=request.state.request_id)


@router.post(
    "/{assistant_message_id}/regenerate",
    response_model=ApiResponse[SendMessageAccepted],
    status_code=status.HTTP_202_ACCEPTED,
)
async def regenerate(
    assistant_message_id: UUID,
    payload: RegenerateRequest,
    request: Request,
    identity: RequestIdentity = Depends(require_generation_identity),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[SendMessageAccepted]:
    result = await container.generation_service.regenerate(
        assistant_message_id=assistant_message_id,
        force_reresolve=payload.force_reresolve,
        user_token=identity.user_token,
        data_access_token=identity.data_access_token or "",
        request_id=request.state.request_id,
        execution_mode=payload.execution_mode,
    )
    CHAT_REQUESTS.labels(operation="REGENERATE", status="accepted").inc()
    return ApiResponse(data=result, request_id=request.state.request_id)


@router.get(
    "/{message_id}/execution-events",
    response_model=ApiResponse[list[ExecutionEventView]],
)
async def execution_events(
    message_id: UUID,
    request: Request,
    response: Response,
    include_reasoning: bool = Query(default=False),
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=2000, ge=1, le=10000),
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[list[ExecutionEventView]]:
    rows = await container.conversation_service.message_execution_events(
        message_id, identity.user_token, after_sequence=after_sequence, limit=limit+1
    )
    response.headers["X-Has-More"] = "true" if len(rows)>limit else "false"
    rows = rows[:limit]
    response.headers["X-Next-After-Sequence"] = str(rows[-1].sequence_no if rows else after_sequence)
    if not include_reasoning or not container.settings.stream_reasoning_enabled:
        rows = [r for r in rows if not r.event_type.startswith("answer.reasoning.")]
    return ApiResponse(
        data=[ExecutionEventView.model_validate(row) for row in rows],
        request_id=request.state.request_id,
    )


@router.get("/{message_id}/alternatives", response_model=ApiResponse[list[MessageView]])
async def alternatives(
    message_id: UUID,
    request: Request,
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[list[MessageView]]:
    messages = await container.conversation_service.alternatives(
        message_id, identity.user_token
    )
    count = len(messages)
    views: list[MessageView] = []
    for index, message in enumerate(messages, start=1):
        view = MessageView.model_validate(message)
        view.alternative_index = index
        view.alternative_count = count
        views.append(view)
    return ApiResponse(data=views, request_id=request.state.request_id)
