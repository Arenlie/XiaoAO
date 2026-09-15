from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, Request, UploadFile, status
from fastapi.responses import Response

from app.api.dependencies import RequestIdentity, get_container, require_user
from app.attachments.http_headers import content_disposition
from app.container import AppContainer
from app.schemas.attachment import AttachmentView
from app.schemas.common import ApiResponse

router = APIRouter(prefix="/files", tags=["files"])



@router.post(
    "",
    response_model=ApiResponse[AttachmentView],
    status_code=status.HTTP_201_CREATED,
)
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[AttachmentView]:
    view = await container.attachment_service.upload(file=file, user_token=identity.user_token)
    return ApiResponse(data=view, request_id=request.state.request_id)


@router.get("/{attachment_id}", response_model=ApiResponse[AttachmentView])
async def get_file(
    attachment_id: UUID,
    request: Request,
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[AttachmentView]:
    row = await container.attachment_service.get_owned(
        attachment_id=attachment_id, user_token=identity.user_token
    )
    return ApiResponse(data=container.attachment_service.view(row), request_id=request.state.request_id)


@router.get("/{attachment_id}/content")
async def get_file_content(
    attachment_id: UUID,
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> Response:
    descriptor, data = await container.attachment_service.read_owned(
        attachment_id=attachment_id, user_token=identity.user_token
    )
    safe_inline_mimes = {"image/jpeg", "image/png", "image/webp", "image/gif", "application/pdf", "text/plain"}
    inline = descriptor.mime_type in safe_inline_mimes
    return Response(
        content=data,
        media_type=descriptor.mime_type,
        headers={
            "Content-Disposition": content_disposition(descriptor.filename, inline=inline),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=300",
            "Content-Security-Policy": "sandbox; default-src 'none'; img-src data: blob:; style-src 'unsafe-inline'",
            "Cross-Origin-Resource-Policy": "same-origin",
        },
    )


@router.delete("/{attachment_id}", response_model=ApiResponse[dict])
async def delete_file(
    attachment_id: UUID,
    request: Request,
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    await container.attachment_service.delete_owned(
        attachment_id=attachment_id, user_token=identity.user_token
    )
    return ApiResponse(data={"attachment_id": str(attachment_id), "deleted": True}, request_id=request.state.request_id)
