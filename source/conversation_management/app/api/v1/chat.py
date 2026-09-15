from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request, status

from app.api.dependencies import RequestIdentity, get_container, require_generation_identity
from app.container import AppContainer
from app.metrics import CHAT_REQUESTS
from app.schemas.common import ApiResponse
from app.schemas.message import SendMessageAccepted, SendMessageRequest

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post(
    "/messages",
    response_model=ApiResponse[SendMessageAccepted],
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_message(
    payload: SendMessageRequest,
    request: Request,
    identity: RequestIdentity = Depends(require_generation_identity),
    container: AppContainer = Depends(get_container),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ApiResponse[SendMessageAccepted]:
    try:
        accepted = await container.generation_service.send(
            client_session_id=payload.client_session_id,
            user_token=identity.user_token,
            data_access_token=identity.data_access_token or "",
            app_code=payload.app_code,
            content=payload.content.strip(),
            conversation_id=payload.conversation_id,
            idempotency_key=idempotency_key,
            request_id=request.state.request_id,
            attachment_ids=payload.attachments,
            execution_mode=payload.execution_mode,
        )
        CHAT_REQUESTS.labels(operation="SEND", status="accepted").inc()
        return ApiResponse(data=accepted, request_id=request.state.request_id)
    except Exception:
        CHAT_REQUESTS.labels(operation="SEND", status="failed").inc()
        raise
