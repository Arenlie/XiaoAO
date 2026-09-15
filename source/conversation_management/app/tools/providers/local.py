from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.tools.contracts import ToolCallRequest, ToolCallResult, ToolDescriptor

LocalHandler = Callable[[ToolCallRequest, str | None], Awaitable[ToolCallResult]]


class LocalToolProvider:
    def __init__(self) -> None:
        self._handlers: dict[str, LocalHandler] = {}

    def register(self, tool_id: str, handler: LocalHandler) -> None:
        self._handlers[tool_id] = handler

    async def call(self, descriptor: ToolDescriptor, request: ToolCallRequest, *, data_access_token: str | None) -> ToolCallResult:
        handler = self._handlers.get(descriptor.tool_id)
        if handler is None:
            return ToolCallResult(
                tool_id=descriptor.tool_id,
                status="FAILED",
                error_code="LOCAL_TOOL_HANDLER_NOT_FOUND",
                error_message="本地工具处理器未注册",
            )
        return await handler(request, data_access_token)
