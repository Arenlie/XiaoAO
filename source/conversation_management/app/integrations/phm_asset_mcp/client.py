from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.integrations.phm_asset_mcp.errors import (
    PhmAssetMcpError,
    PhmAssetMcpTimeout,
    PhmAssetMcpUnavailable,
)

EXPECTED_TOOLS = {
    "resolve_entity",
    "query_equipment_info",
    "query_space_tree",
    "query_space_children",
    "query_devices",
    "query_scope_collection",
    "query_points",
}


class PhmAssetMcpClient:
    """Thin Streamable HTTP client for phm-asset-mcp.

    Asset MCP uses business statuses such as RESOLVED / NEEDS_DISAMBIGUATION /
    ENTITY_NOT_FOUND while still returning ``success=true``.  Only transport/tool
    errors and ``success=false`` are raised here; business statuses are returned to
    the caller unchanged.
    """

    def __init__(
        self,
        *,
        url: str,
        timeout_seconds: float = 60.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.http_client = http_client

    @staticmethod
    def _tool_error_text(result: Any) -> str:
        parts: list[str] = []
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if text:
                parts.append(str(text))
        return " ".join(parts).strip() or "MCP Tool 执行失败"

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            from mcp import Client
        except ImportError as exc:  # pragma: no cover
            raise PhmAssetMcpError(
                "MCP_SDK_NOT_INSTALLED",
                "未安装 MCP Python SDK v2；请安装 mcp==2.0.0",
            ) from exc

        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with Client(self.url) as client:
                    result = await client.call_tool(name, arguments)
        except TimeoutError as exc:
            raise PhmAssetMcpTimeout(name) from exc
        except PhmAssetMcpError:
            raise
        except Exception as exc:
            raise PhmAssetMcpUnavailable(name, str(exc)) from exc

        if bool(getattr(result, "is_error", False)):
            raise PhmAssetMcpError(
                "PHM_ASSET_MCP_TOOL_ERROR",
                f"{name}: {self._tool_error_text(result)}",
            )

        payload = getattr(result, "structured_content", None)
        if not isinstance(payload, dict):
            raise PhmAssetMcpError(
                "PHM_ASSET_MCP_INVALID_RESULT",
                f"{name}: 返回结果缺少 structured_content",
            )
        if payload.get("success") is False:
            structured_error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            raise PhmAssetMcpError(
                str(structured_error.get("code") or payload.get("status") or "PHM_ASSET_QUERY_FAILED"),
                f"{name}: {structured_error.get('operator_message') or payload.get('message') or '资产查询失败'}",
                public_message=structured_error.get("public_message") or payload.get("message"),
                retryable=structured_error.get("retryable") is True,
                details=structured_error,
            )
        return payload

    async def list_tools(self) -> set[str]:
        try:
            from mcp import Client
        except ImportError as exc:  # pragma: no cover
            raise PhmAssetMcpError(
                "MCP_SDK_NOT_INSTALLED",
                "未安装 MCP Python SDK v2；请安装 mcp==2.0.0",
            ) from exc
        try:
            async with asyncio.timeout(min(self.timeout_seconds, 15.0)):
                async with Client(self.url) as client:
                    result = await client.list_tools()
        except TimeoutError as exc:
            raise PhmAssetMcpTimeout("list_tools") from exc
        except Exception as exc:
            raise PhmAssetMcpUnavailable("list_tools", str(exc)) from exc
        return {str(tool.name) for tool in result.tools}

    async def verify_expected_tools(self) -> set[str]:
        actual = await self.list_tools()
        missing = EXPECTED_TOOLS - actual
        if missing:
            raise PhmAssetMcpError(
                "PHM_ASSET_MCP_TOOLS_MISSING",
                f"PHM Asset MCP 缺少工具: {sorted(missing)}",
            )
        return actual

    async def ready(self, ready_url: str) -> dict[str, Any]:
        if self.http_client is None:
            async with httpx.AsyncClient() as client:
                response = await client.get(ready_url, timeout=min(self.timeout_seconds, 10.0))
        else:
            response = await self.http_client.get(
                ready_url,
                timeout=min(self.timeout_seconds, 10.0),
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise PhmAssetMcpError(
                "PHM_ASSET_MCP_READY_INVALID",
                "PHM Asset MCP /ready 返回格式异常",
            )
        return payload

    async def resolve_entity(self, **kwargs: Any) -> dict[str, Any]:
        return await self.call_tool("resolve_entity", kwargs)

    async def lookup(self, **kwargs: Any):
        from app.integrations.phm_asset_mcp.entity_adapter import adapt_asset_resolution

        return adapt_asset_resolution(await self.resolve_entity(**kwargs))

    async def query_equipment_info(self, **kwargs: Any) -> dict[str, Any]:
        return await self.call_tool("query_equipment_info", kwargs)

    async def query_space_tree(self, **kwargs: Any) -> dict[str, Any]:
        return await self.call_tool("query_space_tree", kwargs)

    async def query_space_children(self, **kwargs: Any) -> dict[str, Any]:
        return await self.call_tool("query_space_children", kwargs)

    async def query_devices(self, **kwargs: Any) -> dict[str, Any]:
        return await self.call_tool("query_devices", kwargs)

    async def query_scope_collection(self, **kwargs: Any) -> dict[str, Any]:
        return await self.call_tool("query_scope_collection", kwargs)

    async def query_points(self, **kwargs: Any) -> dict[str, Any]:
        return await self.call_tool("query_points", kwargs)
