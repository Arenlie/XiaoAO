from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from app.integrations.phm_feature_mcp.errors import (
    PhmFeatureMcpError,
    PhmFeatureMcpTimeout,
    PhmFeatureMcpUnavailable,
)

EXPECTED_TOOLS = {
    "extract_vibration_features",
    "extract_rotational_speed_feature",
}

ProgressCallback = Callable[[Any, Any, str | None], Awaitable[None] | None]


class PhmFeatureMcpClient:
    """Thin client for phm_feature_mcp 1.0.0.

    Conversation Service never decodes waveform Base64. The client only transports
    structured arguments/results over MCP Streamable HTTP. ``supported=false`` from
    rotational-speed extraction is a successful business result, not an exception.
    """

    def __init__(
        self,
        *,
        url: str,
        feature_timeout_seconds: float = 30.0,
        rpm_timeout_seconds: float = 45.0,
    ) -> None:
        self.url = url
        self.feature_timeout_seconds = feature_timeout_seconds
        self.rpm_timeout_seconds = rpm_timeout_seconds

    @staticmethod
    def _tool_error_text(result: Any) -> str:
        parts: list[str] = []
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if text:
                parts.append(str(text))
        return " ".join(parts).strip() or "MCP Tool 执行失败"

    def _timeout_for(self, name: str) -> float:
        if name == "extract_rotational_speed_feature":
            return self.rpm_timeout_seconds
        return self.feature_timeout_seconds

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        try:
            from mcp import Client
        except ImportError as exc:  # pragma: no cover
            raise PhmFeatureMcpError(
                "MCP_SDK_NOT_INSTALLED",
                "未安装 MCP Python SDK v2；请安装 mcp==2.0.0",
            ) from exc

        timeout_seconds = self._timeout_for(name)
        try:
            async with asyncio.timeout(timeout_seconds):
                async with Client(self.url) as client:
                    kwargs: dict[str, Any] = {}
                    if progress_callback is not None:
                        kwargs["progress_callback"] = progress_callback
                    result = await client.call_tool(name, arguments, **kwargs)
        except TimeoutError as exc:
            raise PhmFeatureMcpTimeout(name) from exc
        except PhmFeatureMcpError:
            raise
        except Exception as exc:
            raise PhmFeatureMcpUnavailable(name, str(exc)) from exc

        if bool(getattr(result, "is_error", False)):
            raise PhmFeatureMcpError(
                "PHM_FEATURE_MCP_TOOL_ERROR",
                f"{name}: {self._tool_error_text(result)}",
            )

        payload = getattr(result, "structured_content", None)
        if not isinstance(payload, dict):
            raise PhmFeatureMcpError(
                "PHM_FEATURE_MCP_INVALID_RESULT",
                f"{name}: 返回结果缺少 structured_content",
            )

        # Current 1.0.1 primarily reports validation/execution errors through MCP
        # Tool errors. If a future compatible version additionally returns
        # success=false, treat it as a business execution failure. Do NOT treat
        # supported=false as an error: it means no reliable RPM conclusion.
        if payload.get("success") is False:
            raise PhmFeatureMcpError(
                "PHM_FEATURE_MCP_EXECUTION_ERROR",
                f"{name}: {payload.get('error') or '特征提取执行失败'}",
            )
        return payload

    async def list_tools(self) -> set[str]:
        try:
            from mcp import Client
        except ImportError as exc:  # pragma: no cover
            raise PhmFeatureMcpError(
                "MCP_SDK_NOT_INSTALLED",
                "未安装 MCP Python SDK v2；请安装 mcp==2.0.0",
            ) from exc
        try:
            async with asyncio.timeout(min(max(self.rpm_timeout_seconds, 5.0), 15.0)):
                async with Client(self.url) as client:
                    result = await client.list_tools()
        except TimeoutError as exc:
            raise PhmFeatureMcpTimeout("list_tools") from exc
        except Exception as exc:
            raise PhmFeatureMcpUnavailable("list_tools", str(exc)) from exc
        return {str(tool.name) for tool in result.tools}

    async def verify_expected_tools(self) -> set[str]:
        actual = await self.list_tools()
        missing = EXPECTED_TOOLS - actual
        if missing:
            raise PhmFeatureMcpError(
                "PHM_FEATURE_MCP_TOOLS_MISSING",
                f"PHM Feature MCP 缺少工具: {sorted(missing)}",
            )
        return actual

    async def extract_vibration_features(self, **kwargs: Any) -> dict[str, Any]:
        return await self.call_tool("extract_vibration_features", kwargs)

    async def extract_rotational_speed_feature(self, **kwargs: Any) -> dict[str, Any]:
        return await self.call_tool("extract_rotational_speed_feature", kwargs)
