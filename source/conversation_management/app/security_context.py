from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator

_current_user_token: ContextVar[str | None] = ContextVar("current_user_token", default=None)
_rls_bypass: ContextVar[bool] = ContextVar("rls_bypass", default=False)
_current_request_id: ContextVar[str | None] = ContextVar("current_request_id", default=None)


def get_current_user_token() -> str | None:
    return _current_user_token.get()


def is_rls_bypass() -> bool:
    return _rls_bypass.get()


def get_current_request_id() -> str | None:
    return _current_request_id.get()


@contextmanager
def request_security_scope(
    *, user_token: str | None, request_id: str | None = None
) -> Iterator[None]:
    user_handle: Token[str | None] = _current_user_token.set(user_token)
    request_handle: Token[str | None] = _current_request_id.set(request_id)
    try:
        yield
    finally:
        _current_request_id.reset(request_handle)
        _current_user_token.reset(user_handle)


@contextmanager
def tenant_scope(user_token: str) -> Iterator[None]:
    handle: Token[str | None] = _current_user_token.set(user_token)
    try:
        yield
    finally:
        _current_user_token.reset(handle)


@contextmanager
def rls_bypass_scope() -> Iterator[None]:
    handle: Token[bool] = _rls_bypass.set(True)
    try:
        yield
    finally:
        _rls_bypass.reset(handle)
