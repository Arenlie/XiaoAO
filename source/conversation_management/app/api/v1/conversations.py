from __future__ import annotations

from collections import defaultdict
import base64
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.dependencies import RequestIdentity, get_container, require_user
from app.container import AppContainer
from app.schemas.common import ApiResponse, CursorApiResponse
from app.schemas.conversation import (
    BranchView,
    ConversationDetail,
    ConversationListItem,
    CreateConversationRequest,
    RenameConversationRequest,
)
from app.schemas.message import MessageView
from app.schemas.task import EntitySelectionView

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.post("", response_model=ApiResponse[ConversationListItem])
async def create_conversation(
    payload: CreateConversationRequest,
    request: Request,
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[ConversationListItem]:
    conversation = await container.conversation_service.create(
        identity.user_token, payload.app_code
    )
    return ApiResponse(
        data=ConversationListItem.model_validate(conversation),
        request_id=request.state.request_id,
    )


@router.get("", response_model=CursorApiResponse[list[ConversationListItem]])
async def list_conversations(
    request: Request,
    app_code: str = Query(default="xiaoao", max_length=64),
    search: str | None = Query(default=None, max_length=100),
    next_cursor: str | None = Query(
        default=None, alias="X-Next-Cursor", max_length=64
    ),
    limit: int = Query(default=30, ge=1, le=100),
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> CursorApiResponse[list[ConversationListItem]]:
    offset = _decode_cursor(next_cursor)
    rows = await container.conversation_service.list(
        identity.user_token, app_code, search, limit + 1, offset
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    encoded_next_cursor = _encode_cursor(offset + limit) if has_more else None
    return CursorApiResponse(
        data=[ConversationListItem.model_validate(row) for row in rows],
        next_cursor=encoded_next_cursor,
        request_id=request.state.request_id,
    )


@router.get("/{conversation_id}", response_model=ApiResponse[ConversationDetail])
async def get_conversation(
    conversation_id: UUID,
    request: Request,
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[ConversationDetail]:
    conversation = await container.conversation_service.get(
        conversation_id, identity.user_token
    )
    messages = await container.conversation_service.messages(
        conversation_id, identity.user_token
    )
    views = _message_views(messages)
    detail = ConversationDetail(
        **ConversationListItem.model_validate(conversation).model_dump(),
        app_code=conversation.app_code,
        status=conversation.status,
        messages=views,
    )
    return ApiResponse(data=detail, request_id=request.state.request_id)


@router.patch("/{conversation_id}", response_model=ApiResponse[ConversationListItem])
async def rename_conversation(
    conversation_id: UUID,
    payload: RenameConversationRequest,
    request: Request,
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[ConversationListItem]:
    conversation = await container.conversation_service.rename(
        conversation_id, identity.user_token, payload.title
    )
    return ApiResponse(
        data=ConversationListItem.model_validate(conversation),
        request_id=request.state.request_id,
    )


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: UUID,
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> Response:
    await container.conversation_service.delete(conversation_id, identity.user_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{conversation_id}/pin", response_model=ApiResponse[ConversationListItem])
async def pin_conversation(
    conversation_id: UUID,
    request: Request,
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[ConversationListItem]:
    conversation = await container.conversation_service.pin(
        conversation_id, identity.user_token, True
    )
    return ApiResponse(
        data=ConversationListItem.model_validate(conversation),
        request_id=request.state.request_id,
    )


@router.delete("/{conversation_id}/pin", response_model=ApiResponse[ConversationListItem])
async def unpin_conversation(
    conversation_id: UUID,
    request: Request,
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[ConversationListItem]:
    conversation = await container.conversation_service.pin(
        conversation_id, identity.user_token, False
    )
    return ApiResponse(
        data=ConversationListItem.model_validate(conversation),
        request_id=request.state.request_id,
    )


@router.get(
    "/{conversation_id}/pending-entity-selection",
    response_model=ApiResponse[EntitySelectionView | None],
)
async def pending_entity_selection(
    conversation_id: UUID,
    request: Request,
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[EntitySelectionView | None]:
    payload = await container.task_service.get_pending_selection_for_conversation(
        conversation_id, identity.user_token
    )
    return ApiResponse(
        data=EntitySelectionView.model_validate(payload) if payload is not None else None,
        request_id=request.state.request_id,
    )


@router.get("/{conversation_id}/messages", response_model=ApiResponse[list[MessageView]])
async def list_messages(
    conversation_id: UUID,
    request: Request,
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[list[MessageView]]:
    messages = await container.conversation_service.messages(
        conversation_id, identity.user_token
    )
    return ApiResponse(data=_message_views(messages), request_id=request.state.request_id)


@router.get("/{conversation_id}/branches", response_model=ApiResponse[list[BranchView]])
async def list_branches(
    conversation_id: UUID,
    request: Request,
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[list[BranchView]]:
    branches = await container.conversation_service.branches(
        conversation_id, identity.user_token
    )
    return ApiResponse(
        data=[BranchView.model_validate(item) for item in branches],
        request_id=request.state.request_id,
    )


@router.post(
    "/{conversation_id}/branches/{branch_id}/activate",
    response_model=ApiResponse[BranchView],
)
async def activate_branch(
    conversation_id: UUID,
    branch_id: UUID,
    request: Request,
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[BranchView]:
    branch = await container.conversation_service.activate_branch(
        conversation_id, branch_id, identity.user_token
    )
    return ApiResponse(
        data=BranchView.model_validate(branch), request_id=request.state.request_id
    )


def _message_views(messages) -> list[MessageView]:
    groups: dict[tuple[object, str], list] = defaultdict(list)
    for message in messages:
        groups[(message.parent_message_id, message.role)].append(message)
    views: list[MessageView] = []
    for message in messages:
        siblings = groups[(message.parent_message_id, message.role)]
        siblings.sort(key=lambda item: item.created_at)
        view = MessageView.model_validate(message)
        view.alternative_count = len(siblings)
        view.alternative_index = siblings.index(message) + 1
        views.append(view)
    return views


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padding = "=" * (-len(cursor) % 4)
        value = int(base64.urlsafe_b64decode(cursor + padding).decode())
        return max(value, 0)
    except (ValueError, UnicodeDecodeError):
        return 0
