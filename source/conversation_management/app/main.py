from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.v1.health import router as health_router
from app.api.v1.router import router as chat_v1_router
from app.config import get_settings
from app.container import create_container
from app.domain.exceptions import AppError
from app.logging import configure_logging
from app.security_context import request_security_scope
from app.telemetry import current_trace_ids, instrument_fastapi

settings = get_settings()
configure_logging(settings, component="api")
log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    container = create_container(settings)
    app.state.container = container
    await container.queue.ensure_groups()
    if settings.phm_asset_mcp_enabled and settings.phm_asset_mcp_verify_tools_on_start:
        await container.phm_asset_mcp_client.verify_expected_tools()
    if settings.phm_data_mcp_enabled and settings.phm_data_mcp_verify_tools_on_start:
        await container.phm_data_mcp_client.verify_expected_tools()
    if (
        settings.phm_diagnosis_mcp_enabled
        and settings.phm_diagnosis_mcp_verify_tools_on_start
    ):
        await container.phm_diagnosis_mcp_client.verify_expected_tools()
    if (
        settings.phm_feature_mcp_enabled
        and settings.phm_feature_mcp_verify_tools_on_start
    ):
        await container.phm_feature_mcp_client.verify_expected_tools()
    log.info("api_started", app=settings.app_name, env=settings.app_env)
    try:
        yield
    finally:
        await container.close()
        log.info("api_stopped")


app = FastAPI(
    title="Conversation Backend",
    version=settings.app_version,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allow_cors_origins,
    allow_credentials=settings.allow_cors_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "X-Trace-ID", "X-SSE-Protocol-Version"],
)


class RequestContextMiddleware:
    """Pure ASGI request context middleware safe for long-lived SSE streams."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = headers.get("X-Request-ID") or f"req_{uuid4().hex}"
        query_params = parse_qs(
            scope.get("query_string", b"").decode("latin-1"),
            keep_blank_values=True,
        )
        raw_user_values = query_params.get("X-User-Token", [])
        raw_query_user_token = (raw_user_values[-1] if raw_user_values else "").strip()
        raw_header_user_token = (headers.get("X-User-Token") or "").strip()
        raw_user_token = raw_query_user_token or raw_header_user_token
        user_token = raw_user_token[:128] if raw_user_token else None
        scope.setdefault("state", {})["request_id"] = request_id
        structlog.contextvars.bind_contextvars(
            request_id=request_id, user_token=user_token or ""
        )

        async def send_with_context(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Request-ID"] = request_id
                trace_id, _ = current_trace_ids()
                if trace_id:
                    response_headers["X-Trace-ID"] = trace_id
                request_path = str(scope.get("path", ""))
                if request_path == "/chat" or request_path.startswith("/chat/"):
                    response_headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                    response_headers["Pragma"] = "no-cache"
                    response_headers["Expires"] = "0"
                    response_headers["Referrer-Policy"] = "no-referrer"
            await send(message)

        try:
            with request_security_scope(user_token=user_token, request_id=request_id):
                await self.app(scope, receive, send_with_context)
        finally:
            structlog.contextvars.clear_contextvars()


app.add_middleware(RequestContextMiddleware)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> ORJSONResponse:
    trace_id, _ = current_trace_ids()
    return ORJSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "data": None,
            "message": exc.message,
            "error_code": exc.code,
            "request_id": getattr(request.state, "request_id", ""),
            "trace_id": trace_id or None,
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> ORJSONResponse:
    trace_id, _ = current_trace_ids()
    return ORJSONResponse(
        status_code=422,
        content={
            "success": False,
            "data": None,
            "message": "请求参数校验失败",
            "error_code": "VALIDATION_ERROR",
            "details": exc.errors(),
            "request_id": getattr(request.state, "request_id", ""),
            "trace_id": trace_id or None,
        },
    )


app.include_router(chat_v1_router, prefix=settings.api_prefix)
app.include_router(health_router)
instrument_fastapi(app, settings)


WEB_ROOT = Path(__file__).resolve().parent / "web"


@app.get("/", include_in_schema=False)
async def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/chat/")


app.mount(
    "/controller",
    StaticFiles(directory=WEB_ROOT / "controller", html=True),
    name="controller-ui",
)
app.mount(
    "/chat",
    StaticFiles(directory=WEB_ROOT / "chat", html=True),
    name="chat-ui",
)
