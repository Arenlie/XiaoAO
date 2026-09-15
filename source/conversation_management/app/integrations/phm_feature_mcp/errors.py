from __future__ import annotations


class PhmFeatureMcpError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class PhmFeatureMcpTimeout(PhmFeatureMcpError):
    def __init__(self, tool_name: str) -> None:
        super().__init__(
            "PHM_FEATURE_MCP_TIMEOUT",
            f"PHM Feature MCP 调用超时: {tool_name}",
        )


class PhmFeatureMcpUnavailable(PhmFeatureMcpError):
    def __init__(self, tool_name: str, detail: str) -> None:
        super().__init__(
            "PHM_FEATURE_MCP_UNAVAILABLE",
            f"PHM Feature MCP 连接失败: {tool_name}: {detail}",
        )
