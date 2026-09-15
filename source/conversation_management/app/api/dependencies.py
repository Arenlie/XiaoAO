from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, Query, Request

from app.container import AppContainer
from app.domain.exceptions import AppError


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    user_token: str
    data_access_token: str | None


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


def get_request_id(request: Request) -> str:
    return request.state.request_id


def _resolve_user_token(query_value: str | None, header_value: str | None) -> str:
    query_token = (query_value or "").strip()
    header_token = (header_value or "").strip()
    if query_token and header_token and query_token != header_token:
        raise AppError("USER_TOKEN_CONFLICT", "X-User-Token 查询参数与请求头不一致", 400)
    value = query_token or header_token
    if not value:
        raise AppError("USER_TOKEN_MISSING", "缺少 X-User-Token", 401)
    if len(value) > 128:
        raise AppError("USER_TOKEN_INVALID", "X-User-Token 长度超过限制", 400)
    return value


async def require_user(
    x_user_token_query: str | None = Query(default=None, alias="X-User-Token"),
    x_user_token_header: str | None = Header(default=None, alias="X-User-Token"),
) -> RequestIdentity:
    """Resolve stable user identity from query or header.

    Native EventSource cannot attach custom headers, so query-string auth remains
    fully supported. Fetch/axios based formal frontends may instead send the same
    token in the X-User-Token header.
    """
    return RequestIdentity(
        user_token=_resolve_user_token(x_user_token_query, x_user_token_header),
        data_access_token=None,
    )


async def require_generation_identity(
    x_user_token_query: str | None = Query(default=None, alias="X-User-Token"),
    x_user_token_header: str | None = Header(default=None, alias="X-User-Token"),
    token: str | None = Header(default=None, alias="Token"),
) -> RequestIdentity:
    """Authenticate generation requests.

    Stable user identity may be supplied either in the URL query or in the
    X-User-Token header; the data-access credential remains in Token.
    """
    normalized_token = (token or "").strip()
    if not normalized_token:
        raise AppError("DATA_ACCESS_TOKEN_MISSING", "缺少 Token", 401)
    return RequestIdentity(
        user_token=_resolve_user_token(x_user_token_query, x_user_token_header),
        data_access_token=normalized_token,
    )


async def require_controller_admin(
    request: Request,
    controller_token: str | None = Header(default=None, alias="Controller-Token"),
    identity: RequestIdentity = Depends(require_user),
) -> RequestIdentity:
    """Protect controller mutation and inspection endpoints with a separate secret."""
    import hmac

    expected = request.app.state.container.settings.controller_admin_token
    if not expected:
        raise AppError(
            "CONTROLLER_ADMIN_TOKEN_NOT_CONFIGURED",
            "服务端未配置 CONTROLLER_ADMIN_TOKEN",
            503,
        )
    if not controller_token or not hmac.compare_digest(controller_token.strip(), expected):
        raise AppError("CONTROLLER_UNAUTHORIZED", "Controller-Token 无效", 401)
    return identity
