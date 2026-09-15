from __future__ import annotations


class PhmDiagnosisMcpError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class PhmDiagnosisMcpTimeout(PhmDiagnosisMcpError):
    def __init__(self, tool_name: str) -> None:
        super().__init__(
            "PHM_DIAGNOSIS_MCP_TIMEOUT",
            f"PHM Diagnosis MCP 调用超时: {tool_name}",
        )


class PhmDiagnosisMcpUnavailable(PhmDiagnosisMcpError):
    def __init__(self, tool_name: str, detail: str) -> None:
        super().__init__(
            "PHM_DIAGNOSIS_MCP_UNAVAILABLE",
            f"PHM Diagnosis MCP 连接失败: {tool_name}: {detail}",
        )
