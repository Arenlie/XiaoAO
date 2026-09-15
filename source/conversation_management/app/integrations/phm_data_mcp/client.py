from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.integrations.phm_data_mcp.errors import (
    PhmDataMcpError,
    PhmDataMcpTimeout,
    PhmDataMcpUnavailable,
)

logger = logging.getLogger(__name__)

EXPECTED_TOOLS = {
    "get_waveform",
    "get_feature_trend",
    "get_temperature_trend",
    "check_data_availability",
    "get_data_snapshot",
    "get_device_data",
    "query_alarm_records",
    "query_health_score",
}


class PhmDataMcpClient:
    """Thin PHM Data MCP 1.0.0-compatible client.

    The client deliberately keeps MCP lifecycle local to one call. It performs the two
    required error checks: MCP protocol/tool failure first, then the PHM service's own
    ``success`` flag. Binary payloads are returned untouched; this layer never decodes
    or re-encodes Base64 waveform/trend data.
    """

    def __init__(
        self,
        *,
        url: str,
        timeout_seconds: float = 120.0,
        device_timeout_seconds: float | None = None,
        http_client: httpx.AsyncClient | None = None,
        close_timeout_seconds: float = 5.0,
    ) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.device_timeout_seconds = float(device_timeout_seconds or timeout_seconds)
        self.http_client = http_client
        self.close_timeout_seconds = max(0.5, float(close_timeout_seconds))

    @staticmethod
    def _tool_error_text(result: Any) -> str:
        parts: list[str] = []
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if text:
                parts.append(str(text))
        return " ".join(parts).strip() or "MCP Tool 执行失败"

    @staticmethod
    def _device_payload_too_large(result: Any) -> bool:
        text = PhmDataMcpClient._tool_error_text(result)
        payload = getattr(result, "structured_content", None)
        if isinstance(payload, dict):
            text += " " + str(payload.get("error") or "")
        normalized = text.lower()
        return (
            "mcp_max_response_bytes" in normalized
            or "响应超过" in text
            or "response" in normalized and "too large" in normalized
        )

    @staticmethod
    def _device_window_retryable_text(text: str) -> bool:
        normalized = str(text or "").lower()
        return any(
            marker in normalized
            for marker in (
                "timeout",
                "timed out",
                "exceeded time limit",
                "execution timeout",
                "operation exceeded",
                "query exceeded",
                "maxtimems",
                "mcp_max_response_bytes",
                "响应超过",
                "响应预算",
                "趋势点数",
            )
        )


    @staticmethod
    def _device_server_fallback_exhausted_text(text: str) -> bool:
        normalized = str(text or "").lower()
        return any(marker.lower() in normalized for marker in (
            "波形数据本身已超过整设备安全响应预算",
            "即使趋势窗口缩短到",
            "即使趋势窗口缩短到1天仍无法安全返回",
            "all device waveform",
            "safe response budget",
        ))

    @staticmethod
    def _exception_detail(exc: BaseException) -> str:
        """Expand ExceptionGroup/TaskGroup wrappers into actionable child errors."""
        children = getattr(exc, "exceptions", None)
        if children:
            details = [PhmDataMcpClient._exception_detail(child) for child in children]
            details = [item for item in details if item]
            prefix = f"{type(exc).__name__}: {exc}"
            return f"{prefix}; sub-errors=[{' | '.join(details)}]"
        text = str(exc).strip()
        return f"{type(exc).__name__}: {text}" if text else type(exc).__name__

    async def _safe_close_after_result(self, client: Any, *, name: str) -> None:
        """Close one-shot MCP transport without discarding an already received result.

        mcp==2.0.0 Streamable HTTP uses an anyio TaskGroup internally. In production we
        observed call_tool() completing successfully on the server, followed by a TaskGroup
        exception while __aexit__ tears the transport down. Treating that close-only error as
        a business failure caused the whole-device call to be retried three times even though
        Data MCP logged success each time. Once a tool result is in hand, teardown failure is
        logged but must not overwrite that result.
        """
        try:
            await asyncio.wait_for(
                client.__aexit__(None, None, None),
                timeout=self.close_timeout_seconds,
            )
        except BaseException as exc:  # close-only transport failure; result is already safe
            logger.warning(
                "phm_data_mcp_close_after_success_failed tool=%s detail=%s",
                name,
                self._exception_detail(exc),
            )

    async def _invoke_once(
        self,
        client_type: Any,
        name: str,
        arguments: dict[str, Any],
        timeout_seconds: float,
    ) -> Any:
        client = client_type(self.url)
        entered = False
        result: Any = None
        try:
            async with asyncio.timeout(timeout_seconds):
                await client.__aenter__()
                entered = True
                result = await client.call_tool(name, arguments)
        except BaseException:
            if entered:
                # When the actual call failed, preserve the original call exception. Closing
                # is best-effort and must not replace the root cause with a TaskGroup wrapper.
                try:
                    await asyncio.wait_for(
                        client.__aexit__(*__import__("sys").exc_info()),
                        timeout=self.close_timeout_seconds,
                    )
                except BaseException:
                    pass
            raise

        if entered:
            await self._safe_close_after_result(client, name=name)
        return result

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            from mcp import Client
        except ImportError as exc:  # pragma: no cover - deployment dependency guard
            raise PhmDataMcpError(
                "MCP_SDK_NOT_INSTALLED",
                "未安装 MCP Python SDK v2；请安装 mcp==2.0.0",
            ) from exc

        timeout_seconds = (
            self.device_timeout_seconds if name == "get_device_data" else self.timeout_seconds
        )
        attempts: list[dict[str, Any]] = [dict(arguments)]
        if name == "get_device_data":
            if arguments.get("trend_hours") not in (None, ""):
                try:
                    requested_hours = float(arguments["trend_hours"])
                    fallback_hours = float(arguments.get("fallback_trend_hours") or 0)
                except (TypeError, ValueError):
                    requested_hours, fallback_hours = 24.0, 0.0
                if 0 < fallback_hours < requested_hours:
                    retry = dict(arguments)
                    retry["trend_hours"] = fallback_hours
                    retry.pop("fallback_trend_hours", None)
                    attempts.append(retry)
            else:
                try:
                    requested_days = int(arguments.get("trend_days") or 30)
                except (TypeError, ValueError):
                    requested_days = 30
                # Backward-compatible fallback for callers still using whole days.
                for fallback_days in (7, 1):
                    if requested_days > fallback_days:
                        retry = dict(arguments)
                        retry["trend_days"] = fallback_days
                        attempts.append(retry)

        last_result: Any = None
        for index, attempt_arguments in enumerate(attempts):
            try:
                result = await self._invoke_once(
                    Client, name, attempt_arguments, timeout_seconds
                )
            except TimeoutError as exc:
                if name == "get_device_data" and index + 1 < len(attempts):
                    continue
                raise PhmDataMcpTimeout(name) from exc
            except PhmDataMcpError:
                raise
            except Exception as exc:
                detail = self._exception_detail(exc)
                if "SSE stream ended without a response" in detail:
                    # MCP Python SDK v2.0.0 Streamable HTTP can surface this when a
                    # large tools/call result is encoded as one SSE event. The Data MCP
                    # business function may already have completed, so retrying the same
                    # expensive read can duplicate 50-150s work without ever fixing the
                    # transport ceiling. Data MCP >=1.1.5 uses json_response=True.
                    raise PhmDataMcpError(
                        "PHM_DATA_MCP_RESPONSE_STREAM_FAILED",
                        (
                            f"{name}: MCP响应流结束但未收到JSON-RPC结果；"
                            "服务端业务可能已经执行成功。若为大结果，请确认 "
                            "当前 PHM Data MCP 已启用 json_response=True。"
                            f" 原始错误: {detail}"
                        ),
                    ) from exc
                raise PhmDataMcpUnavailable(name, detail) from exc

            last_result = result
            if (
                name == "get_device_data"
                and index + 1 < len(attempts)
                and bool(getattr(result, "is_error", False))
                and (
                    self._device_payload_too_large(result)
                    or self._device_window_retryable_text(self._tool_error_text(result))
                )
                and not self._device_server_fallback_exhausted_text(self._tool_error_text(result))
            ):
                continue

            if bool(getattr(result, "is_error", False)):
                raise PhmDataMcpError(
                    "PHM_DATA_MCP_TOOL_ERROR",
                    f"{name}: {self._tool_error_text(result)}",
                )

            payload = getattr(result, "structured_content", None)
            if not isinstance(payload, dict):
                raise PhmDataMcpError(
                    "PHM_DATA_MCP_INVALID_RESULT",
                    f"{name}: 返回结果缺少 structured_content",
                )
            if not bool(payload.get("success", False)):
                # Some MCP runtimes encode application failure in structured content.
                # Apply the same size fallback before surfacing the error.
                error_text = str(payload.get("error") or "")
                if (
                    name == "get_device_data"
                    and index + 1 < len(attempts)
                    and self._device_window_retryable_text(error_text)
                    and not self._device_server_fallback_exhausted_text(error_text)
                ):
                    continue
                raise PhmDataMcpError(
                    "PHM_DATA_QUERY_FAILED",
                    f"{name}: {error_text or 'PHM 数据查询失败'}",
                )
            if name == "get_device_data" and index > 0:
                payload = dict(payload)
                using_hours = arguments.get("trend_hours") not in (None, "")
                payload["_conversation_adapter"] = {
                    "reason": "device_trend_window_fallback",
                    "all_device_points_preserved": True,
                    "waveforms_requested_as_latest": not bool(arguments.get("target_time")),
                }
                if using_hours:
                    payload["_conversation_adapter"].update(
                        {
                            "requested_trend_hours": float(arguments.get("trend_hours") or 24),
                            "effective_trend_hours": float(
                                attempt_arguments.get("trend_hours") or 12
                            ),
                        }
                    )
                else:
                    payload["_conversation_adapter"].update(
                        {
                            "requested_trend_days": int(arguments.get("trend_days") or 30),
                            "effective_trend_days": int(
                                attempt_arguments.get("trend_days") or 30
                            ),
                        }
                    )
            return payload

        # Defensive fallback; loop normally returns or raises.
        raise PhmDataMcpError(
            "PHM_DATA_QUERY_FAILED",
            f"{name}: {self._tool_error_text(last_result) if last_result is not None else 'PHM 数据查询失败'}",
        )

    async def list_tools(self) -> set[str]:
        try:
            from mcp import Client
        except ImportError as exc:  # pragma: no cover
            raise PhmDataMcpError(
                "MCP_SDK_NOT_INSTALLED",
                "未安装 MCP Python SDK v2；请安装 mcp==2.0.0",
            ) from exc
        client = Client(self.url)
        entered = False
        result: Any = None
        try:
            async with asyncio.timeout(self.timeout_seconds):
                await client.__aenter__()
                entered = True
                result = await client.list_tools()
        except TimeoutError as exc:
            if entered:
                try:
                    await asyncio.wait_for(
                        client.__aexit__(type(exc), exc, exc.__traceback__),
                        timeout=self.close_timeout_seconds,
                    )
                except BaseException:
                    pass
            raise PhmDataMcpTimeout("list_tools") from exc
        except Exception as exc:
            if entered:
                try:
                    await asyncio.wait_for(
                        client.__aexit__(type(exc), exc, exc.__traceback__),
                        timeout=self.close_timeout_seconds,
                    )
                except BaseException:
                    pass
            raise PhmDataMcpUnavailable("list_tools", self._exception_detail(exc)) from exc
        if entered:
            await self._safe_close_after_result(client, name="list_tools")
        return {str(tool.name) for tool in result.tools}

    async def verify_expected_tools(self) -> set[str]:
        actual = await self.list_tools()
        missing = EXPECTED_TOOLS - actual
        if missing:
            raise PhmDataMcpError(
                "PHM_DATA_MCP_TOOLS_MISSING",
                f"PHM Data MCP 缺少工具: {sorted(missing)}",
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
            raise PhmDataMcpError(
                "PHM_DATA_MCP_READY_INVALID",
                "PHM Data MCP /ready 返回格式异常",
            )
        return payload

    async def get_waveform(self, **kwargs: Any) -> dict[str, Any]:
        return await self.call_tool("get_waveform", kwargs)

    async def get_feature_trend(self, **kwargs: Any) -> dict[str, Any]:
        return await self.call_tool("get_feature_trend", kwargs)

    async def get_temperature_trend(self, **kwargs: Any) -> dict[str, Any]:
        return await self.call_tool("get_temperature_trend", kwargs)

    async def check_data_availability(self, **kwargs: Any) -> dict[str, Any]:
        return await self.call_tool("check_data_availability", kwargs)

    async def get_data_snapshot(self, **kwargs: Any) -> dict[str, Any]:
        return await self.call_tool("get_data_snapshot", kwargs)

    async def get_device_data(self, **kwargs: Any) -> dict[str, Any]:
        return await self.call_tool("get_device_data", kwargs)

    async def query_alarm_records(self, **kwargs: Any) -> dict[str, Any]:
        return await self.call_tool("query_alarm_records", kwargs)

    async def query_health_score(self, **kwargs: Any) -> dict[str, Any]:
        return await self.call_tool("query_health_score", kwargs)
