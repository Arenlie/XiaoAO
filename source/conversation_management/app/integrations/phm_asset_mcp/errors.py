from __future__ import annotations

from typing import Any


class PhmAssetMcpError(RuntimeError):
    def __init__(self, code: str, message: str, *, public_message: str | None = None,
                 retryable: bool = False, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.public_message = public_message
        self.retryable = retryable
        self.details = dict(details or {})


class PhmAssetMcpTimeout(PhmAssetMcpError):
    def __init__(self, tool_name: str) -> None:
        super().__init__(
            "PHM_ASSET_MCP_TIMEOUT",
            f"PHM Asset MCP 调用超时: {tool_name}",
            retryable=True,
        )


class PhmAssetMcpUnavailable(PhmAssetMcpError):
    def __init__(self, tool_name: str, detail: str = "") -> None:
        message = f"PHM Asset MCP 当前不可用: {tool_name}"
        if detail:
            message += f" ({detail})"
        super().__init__("PHM_ASSET_MCP_UNAVAILABLE", message, retryable=True)
