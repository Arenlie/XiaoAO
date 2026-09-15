from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings
from app.tools.contracts import ToolCallRequest, ToolCallResult, ToolDescriptor, ToolResultStatus


class DifyWorkflowToolProvider:
    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    def _connection(self, descriptor: ToolDescriptor) -> tuple[str, str, float, float]:
        profile = str(descriptor.metadata.get("config_profile") or "").strip()
        if profile == "comprehensive_alarm":
            return (
                self.settings.comprehensive_alarm_workflow_base_url.rstrip("/"),
                self.settings.comprehensive_alarm_workflow_api_key,
                self.settings.comprehensive_alarm_connect_timeout_seconds,
                self.settings.comprehensive_alarm_workflow_timeout_seconds,
            )
        return (
            str(descriptor.metadata.get("base_url") or "").rstrip("/"),
            str(descriptor.metadata.get("api_key") or ""),
            2.0,
            descriptor.timeout_seconds,
        )

    @staticmethod
    def _extract_result(
        payload: dict[str, Any],
    ) -> tuple[Any, str | None, str, str | None]:
        data = payload.get("data")
        if isinstance(data, dict):
            outputs = data.get("outputs")
            run_id = str(data.get("id") or payload.get("workflow_run_id") or "") or None
            workflow_status = str(data.get("status") or "").strip().lower()
            workflow_error = str(data.get("error") or "").strip() or None
            return outputs if outputs is not None else {}, run_id, workflow_status, workflow_error
        return (
            payload.get("outputs", {}),
            str(payload.get("workflow_run_id") or "") or None,
            str(payload.get("status") or "").strip().lower(),
            str(payload.get("error") or "").strip() or None,
        )

    async def call(
        self,
        descriptor: ToolDescriptor,
        request: ToolCallRequest,
        *,
        data_access_token: str | None,
    ) -> ToolCallResult:
        base_url, api_key, connect_timeout, read_timeout = self._connection(descriptor)
        if not base_url or not api_key:
            return ToolCallResult(
                tool_id=descriptor.tool_id,
                status=ToolResultStatus.FAILED,
                error_code="DIFY_WORKFLOW_NOT_CONFIGURED",
                error_message="Dify Workflow 地址或 API Key 未配置",
            )

        inputs = dict(request.arguments)
        if descriptor.requires_data_access_token:
            inputs[str(descriptor.metadata.get("token_input_name") or "data_access_token")] = (
                data_access_token or ""
            )
        timeout = httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=20.0,
            pool=2.0,
        )
        try:
            response = await self.client.post(
                f"{base_url}/workflows/run",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "inputs": inputs,
                    "response_mode": "blocking",
                    "user": request.user_token,
                },
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Dify Workflow response must be an object")
            outputs, workflow_run_id, workflow_status, workflow_error = self._extract_result(
                payload
            )
            structured = outputs if isinstance(outputs, dict) else {"outputs": outputs}
            output_status = str(structured.get("status") or "").upper()
            workflow_failed = workflow_status in {
                "failed",
                "stopped",
                "error",
                "cancelled",
                "canceled",
            }
            output_failed = output_status in {
                "ERROR",
                "QUERY_ERROR",
                "UPSTREAM_ERROR",
                "FAILED",
            }
            success = not workflow_failed and not output_failed
            error_code = None
            error_message = None
            if not success:
                error_code = (
                    output_status
                    or ("DIFY_WORKFLOW_" + workflow_status.upper())
                    or "DIFY_WORKFLOW_QUERY_FAILED"
                )
                error_message = str(
                    structured.get("message")
                    or workflow_error
                    or "Dify Workflow 查询失败"
                )
            return ToolCallResult(
                tool_id=descriptor.tool_id,
                status=(ToolResultStatus.SUCCESS if success else ToolResultStatus.FAILED),
                content=outputs,
                structured_content=structured,
                error_code=error_code,
                error_message=error_message,
                metadata={
                    "workflow_run_id": workflow_run_id,
                    "workflow_status": workflow_status or None,
                    "database_only": bool(descriptor.metadata.get("database_only")),
                },
            )
        except httpx.TimeoutException as exc:
            return ToolCallResult(
                tool_id=descriptor.tool_id,
                status=ToolResultStatus.FAILED,
                error_code="DIFY_WORKFLOW_TIMEOUT",
                error_message=f"Dify Workflow 调用超时: {exc}",
            )
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:1000]
            status_code = exc.response.status_code
            retryable = status_code in {408, 425, 429} or status_code >= 500
            return ToolCallResult(
                tool_id=descriptor.tool_id,
                status=ToolResultStatus.FAILED,
                error_code=(
                    "DIFY_WORKFLOW_HTTP_RETRYABLE"
                    if retryable
                    else "DIFY_WORKFLOW_HTTP_ERROR"
                ),
                error_message=(
                    f"Dify Workflow HTTP {status_code}: "
                    f"{body or exc.response.reason_phrase}"
                ),
                metadata={"http_status": status_code, "retryable": retryable},
            )
        except (httpx.HTTPError, ValueError) as exc:
            return ToolCallResult(
                tool_id=descriptor.tool_id,
                status=ToolResultStatus.FAILED,
                error_code="DIFY_WORKFLOW_TOOL_FAILED",
                error_message=str(exc),
            )
