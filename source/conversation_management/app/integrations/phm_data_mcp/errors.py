from __future__ import annotations


class PhmDataMcpError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class PhmDataMcpTimeout(PhmDataMcpError):
    def __init__(self, tool_name: str) -> None:
        super().__init__(
            "PHM_DATA_MCP_TIMEOUT",
            f"PHM Data MCP 调用超时: {tool_name}",
        )


class PhmDataMcpUnavailable(PhmDataMcpError):
    def __init__(self, tool_name: str, detail: str) -> None:
        super().__init__(
            "PHM_DATA_MCP_UNAVAILABLE",
            f"PHM Data MCP 连接失败: {tool_name}: {detail}",
        )
