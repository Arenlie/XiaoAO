from __future__ import annotations

import httpx

from app.tools.contracts import ToolCallRequest, ToolCallResult, ToolDescriptor, ToolResultStatus


class HttpToolProvider:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def call(self, descriptor: ToolDescriptor, request: ToolCallRequest, *, data_access_token: str | None) -> ToolCallResult:
        url = str(descriptor.metadata.get("url") or "")
        method = str(descriptor.metadata.get("method") or "POST").upper()
        headers = dict(descriptor.metadata.get("headers") or {})
        if data_access_token and descriptor.requires_data_access_token:
            header_name = str(descriptor.metadata.get("token_header") or "Token")
            headers[header_name] = data_access_token
        try:
            response = await self.client.request(
                method,
                url,
                json=request.arguments,
                headers=headers,
                timeout=descriptor.timeout_seconds,
            )
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError:
                payload = {"text": response.text}
            return ToolCallResult(
                tool_id=descriptor.tool_id,
                status=ToolResultStatus.SUCCESS,
                content=payload,
                structured_content=payload if isinstance(payload, dict) else {},
                metadata={"status_code": response.status_code},
            )
        except httpx.HTTPError as exc:
            return ToolCallResult(
                tool_id=descriptor.tool_id,
                status=ToolResultStatus.FAILED,
                error_code="HTTP_TOOL_FAILED",
                error_message=str(exc),
            )
