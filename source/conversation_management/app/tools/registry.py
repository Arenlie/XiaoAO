from __future__ import annotations

from app.domain.exceptions import AppError
from app.tools.contracts import ToolCallRequest, ToolCallResult, ToolDescriptor, ToolProvider, ToolProviderType


class ToolRegistry:
    def __init__(self) -> None:
        self._descriptors: dict[str, ToolDescriptor] = {}
        self._providers: dict[ToolProviderType, ToolProvider] = {}

    def register_provider(self, provider_type: ToolProviderType, provider: ToolProvider) -> None:
        self._providers[provider_type] = provider

    def register_tool(self, descriptor: ToolDescriptor) -> None:
        if descriptor.tool_id in self._descriptors:
            raise ValueError(f"duplicate tool: {descriptor.tool_id}")
        self._descriptors[descriptor.tool_id] = descriptor

    def list_descriptors(self) -> list[ToolDescriptor]:
        return sorted(self._descriptors.values(), key=lambda item: item.tool_id)

    def get_descriptor(self, tool_id: str) -> ToolDescriptor:
        descriptor = self._descriptors.get(tool_id)
        if descriptor is None:
            raise AppError("TOOL_NOT_FOUND", f"工具未注册: {tool_id}", 404)
        return descriptor

    async def call(
        self,
        request: ToolCallRequest,
        *,
        data_access_token: str | None,
    ) -> ToolCallResult:
        descriptor = self.get_descriptor(request.tool_id)
        if not descriptor.enabled:
            raise AppError("TOOL_DISABLED", f"工具已关闭: {request.tool_id}", 503)
        if descriptor.requires_data_access_token and not data_access_token:
            return ToolCallResult(
                tool_id=request.tool_id,
                status="NEEDS_INPUT",
                requirements=[{"field": "data_access_token", "type": "token", "required": True}],
                error_code="DATA_ACCESS_TOKEN_REQUIRED",
                error_message="工具需要数据访问 Token",
            )
        provider = self._providers.get(descriptor.provider_type)
        if provider is None:
            raise AppError(
                "TOOL_PROVIDER_NOT_FOUND",
                f"工具提供器未注册: {descriptor.provider_type}",
                503,
            )
        return await provider.call(
            descriptor,
            request,
            data_access_token=data_access_token,
        )
