from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, WebSocket

from app.api.dependencies import RequestIdentity, get_container, require_user
from app.container import AppContainer
from app.schemas.asr import AsrStatusResponse, AsrTranscriptionResponse
from app.schemas.common import ApiResponse

router = APIRouter(prefix="/asr", tags=["asr"])


@router.get("/status", response_model=ApiResponse[AsrStatusResponse])
async def asr_status(
    request: Request,
    identity: RequestIdentity = Depends(require_user),  # noqa: ARG001
    container: AppContainer = Depends(get_container),
) -> ApiResponse[AsrStatusResponse]:
    settings = container.settings
    return ApiResponse(
        data=AsrStatusResponse(
            enabled=settings.asr_enabled,
            provider=settings.asr_provider,
            model=settings.asr_model,
            max_file_bytes=settings.asr_max_file_bytes,
            default_language=settings.asr_default_language or None,
            realtime_enabled=settings.asr_realtime_enabled,
            realtime_provider=settings.asr_realtime_provider,
            realtime_model=settings.asr_realtime_model,
            realtime_websocket_path="/chat/v1/asr/realtime",
        ),
        request_id=request.state.request_id,
    )


@router.post("/transcriptions", response_model=ApiResponse[AsrTranscriptionResponse])
async def transcribe_audio(
    request: Request,
    file: UploadFile = File(..., description="待识别音频文件"),
    language: str | None = Form(default=None, description="可选，例如 zh/en；留空使用服务端默认值"),
    context: str | None = Form(default=None, description="可选，工业术语/设备名称等识别上下文"),
    enable_itn: bool | None = Form(default=None, description="是否把中文/英文数字规范化为阿拉伯数字"),
    identity: RequestIdentity = Depends(require_user),  # noqa: ARG001
    container: AppContainer = Depends(get_container),
) -> ApiResponse[AsrTranscriptionResponse]:
    limit = container.settings.asr_max_file_bytes
    audio = await file.read(limit + 1)
    result = await container.asr_service.transcribe(
        audio=audio,
        mime_type=file.content_type or "application/octet-stream",
        language=language,
        context=context,
        enable_itn=enable_itn,
    )
    return ApiResponse(
        data=AsrTranscriptionResponse(
            text=result.text,
            language=result.language,
            emotion=result.emotion,
            duration_seconds=result.duration_seconds,
            provider=result.provider,
            model=result.model,
            upstream_request_id=result.upstream_request_id,
            filename=file.filename,
        ),
        request_id=request.state.request_id,
    )


@router.websocket("/realtime")
async def realtime_transcription(
    websocket: WebSocket,
    x_user_token: str = Query(alias="X-User-Token", min_length=1, max_length=128),
    language: str | None = Query(default=None, max_length=16),
) -> None:
    # Authentication follows the existing Chat API convention: a stable user token
    # is supplied in the URL query. The DashScope API key stays server-side.
    _ = x_user_token.strip()
    await websocket.accept()
    container: AppContainer = websocket.app.state.container
    await container.asr_realtime_proxy.run(websocket, language=language)
