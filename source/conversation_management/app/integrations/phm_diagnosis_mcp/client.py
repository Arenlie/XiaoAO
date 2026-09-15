from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping

import httpx

from app.integrations.phm_diagnosis_mcp.errors import (
    PhmDiagnosisMcpError,
    PhmDiagnosisMcpTimeout,
    PhmDiagnosisMcpUnavailable,
)

EXPECTED_TOOLS = {
    "analyze_chart",
    "diagnose_point",
    "diagnose_device",
    "get_model_admission",
}

DIAGNOSIS_CLIENT_CONTRACT_VERSION = "phm-diagnosis-mcp-1.0.0"


class PhmDiagnosisMcpClient:
    """Thin client for PHM Diagnosis MCP 1.0.0.

    Raw waveform/trend payloads are intentionally preserved. The Diagnosis MCP owns
    numeric decoding for diagnosis algorithms; Conversation only validates structure.
    """

    def __init__(
        self,
        *,
        url: str,
        timeout_seconds: float = 120.0,
        device_timeout_seconds: float | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.device_timeout_seconds = float(device_timeout_seconds or timeout_seconds)
        self.http_client = http_client

    @staticmethod
    def _effective_data(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {}
        nested = value.get("data")
        if isinstance(nested, Mapping):
            return dict(nested)
        return dict(value)

    @classmethod
    def _normalize_waveform(cls, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, Mapping):
            return None
        original = dict(value)
        data = cls._effective_data(original)
        encoded = data.get("values_base64") or data.get("float32_base64")
        try:
            fs_hz = float(data.get("sample_rate_hz") or data.get("fs_hz") or 0.0)
        except (TypeError, ValueError):
            fs_hz = 0.0
        if not isinstance(encoded, str) or not encoded.strip() or fs_hz <= 0:
            return None
        if "data" not in original and not original.get("values_base64") and isinstance(original.get("float32_base64"), str):
            original["values_base64"] = original["float32_base64"]
        return original

    @classmethod
    def _waveform_manifest(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {"valid": False, "type": type(value).__name__}
        data = cls._effective_data(value)
        encoded = data.get("values_base64") or data.get("float32_base64")
        try:
            fs_hz = float(data.get("sample_rate_hz") or data.get("fs_hz") or 0.0)
        except (TypeError, ValueError):
            fs_hz = 0.0
        return {
            "point_no": data.get("point_no"),
            "sample_rate_hz": fs_hz,
            "base64_chars": len(encoded) if isinstance(encoded, str) else 0,
            "valid": isinstance(encoded, str) and bool(encoded.strip()) and fs_hz > 0,
        }

    @classmethod
    def _trend_manifest(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            return {"valid": False, "type": type(value).__name__}
        data = cls._effective_data(value)
        series = data.get("series")
        payload = data.get("payload_base64")
        return {
            "point_id": data.get("point_id"),
            "inline_series": isinstance(series, list),
            "series_count": len(series) if isinstance(series, list) else None,
            "payload_base64_chars": len(payload) if isinstance(payload, str) else 0,
            "valid": isinstance(series, list) or (isinstance(payload, str) and bool(payload.strip())),
        }

    @classmethod
    def _normalize_point(cls, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, Mapping):
            return None
        point = dict(value)
        point_no = str(point.get("point_no") or "").strip()
        if not point_no:
            return None
        if "waveform" in point:
            waveform = cls._normalize_waveform(point.get("waveform"))
            if waveform is None:
                point.pop("waveform", None)
            else:
                point["waveform"] = waveform
        for key in ("feature_trends", "temperature_trends"):
            items = point.get(key)
            if items is None:
                continue
            point[key] = [dict(x) for x in items if isinstance(x, Mapping)] if isinstance(items, list) else []
        if not point.get("waveform") and not point.get("feature_trends") and not point.get("temperature_trends"):
            return None
        return point

    @classmethod
    def _argument_manifest(cls, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name == "analyze_chart":
            return {
                "chart_type": arguments.get("chart_type"),
                "waveform": cls._waveform_manifest(arguments.get("waveform")) if "waveform" in arguments else None,
                "trend": cls._trend_manifest(arguments.get("trend")) if "trend" in arguments else None,
                "speed_rpm": arguments.get("speed_rpm"),
            }
        if name == "diagnose_point":
            return {
                "point_no": arguments.get("point_no"),
                "waveform": cls._waveform_manifest(arguments.get("waveform")) if "waveform" in arguments else None,
                "feature_trend_count": len(arguments.get("feature_trends") or []),
                "temperature_trend_count": len(arguments.get("temperature_trends") or []),
                "speed_rpm": arguments.get("speed_rpm"),
                "use_model": arguments.get("use_model"),
            }
        if name == "diagnose_device":
            points = arguments.get("points") or []
            return {
                "device_code": arguments.get("device_code"),
                "point_count": len(points),
                "points": [
                    {
                        "point_no": p.get("point_no"),
                        "waveform": cls._waveform_manifest(p.get("waveform")) if p.get("waveform") else None,
                        "feature_trend_count": len(p.get("feature_trends") or []),
                        "temperature_trend_count": len(p.get("temperature_trends") or []),
                    }
                    for p in points if isinstance(p, Mapping)
                ],
                "speed_rpm": arguments.get("speed_rpm"),
                "use_model": arguments.get("use_model"),
            }
        return {}

    @classmethod
    def _normalize_arguments(cls, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = dict(arguments)
        if name == "analyze_chart" and "waveform" in result:
            waveform = cls._normalize_waveform(result.get("waveform"))
            if waveform is None:
                raise PhmDiagnosisMcpError(
                    "PHM_DIAGNOSIS_INPUT_INVALID",
                    "analyze_chart: waveform.values_base64或sample_rate_hz缺失",
                )
            result["waveform"] = waveform
        elif name == "diagnose_point":
            if "waveform" in result:
                waveform = cls._normalize_waveform(result.get("waveform"))
                if waveform is None:
                    result.pop("waveform", None)
                else:
                    result["waveform"] = waveform
            if not str(result.get("point_no") or "").strip():
                raise PhmDiagnosisMcpError("PHM_DIAGNOSIS_INPUT_INVALID", "diagnose_point: point_no不能为空")
        elif name == "diagnose_device":
            points = [cls._normalize_point(item) for item in (result.get("points") or [])]
            result["points"] = [item for item in points if item is not None]
            if not str(result.get("device_code") or "").strip():
                raise PhmDiagnosisMcpError("PHM_DIAGNOSIS_INPUT_INVALID", "diagnose_device: device_code不能为空")
            if not result["points"]:
                raise PhmDiagnosisMcpError("PHM_DIAGNOSIS_INPUT_INVALID", "diagnose_device: points不能为空或没有可分析数据")
        return result

    @staticmethod
    def _tool_error_text(result: Any) -> str:
        parts: list[str] = []
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if text:
                parts.append(str(text))
        return " ".join(parts).strip() or "MCP Tool 执行失败"

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        arguments = self._normalize_arguments(name, arguments)
        manifest = self._argument_manifest(name, arguments)
        try:
            from mcp import Client
        except ImportError as exc:
            raise PhmDiagnosisMcpError("MCP_SDK_NOT_INSTALLED", "未安装 MCP Python SDK v2；请安装 mcp==2.0.0") from exc
        timeout_seconds = (
            self.device_timeout_seconds if name == "diagnose_device" else self.timeout_seconds
        )
        try:
            async with asyncio.timeout(timeout_seconds):
                async with Client(self.url) as client:
                    result = await client.call_tool(name, arguments)
        except TimeoutError as exc:
            raise PhmDiagnosisMcpTimeout(name) from exc
        except PhmDiagnosisMcpError:
            raise
        except Exception as exc:
            raise PhmDiagnosisMcpUnavailable(name, str(exc)) from exc

        if bool(getattr(result, "is_error", False)):
            detail = self._tool_error_text(result)
            suffix = f"；input_manifest={json.dumps(manifest, ensure_ascii=False, separators=(',', ':'))}" if manifest else ""
            raise PhmDiagnosisMcpError("PHM_DIAGNOSIS_MCP_TOOL_ERROR", f"{name}: {detail}{suffix}")
        payload = getattr(result, "structured_content", None)
        if not isinstance(payload, dict):
            raise PhmDiagnosisMcpError("PHM_DIAGNOSIS_MCP_INVALID_RESULT", f"{name}: 返回结果缺少 structured_content")
        if payload.get("success") is False:
            suffix = f"；input_manifest={json.dumps(manifest, ensure_ascii=False, separators=(',', ':'))}" if manifest else ""
            raise PhmDiagnosisMcpError("PHM_DIAGNOSIS_FAILED", f"{name}: {payload.get('error') or '专业诊断执行失败'}{suffix}")
        return payload

    async def list_tools(self) -> set[str]:
        try:
            from mcp import Client
        except ImportError as exc:
            raise PhmDiagnosisMcpError("MCP_SDK_NOT_INSTALLED", "未安装 MCP Python SDK v2；请安装 mcp==2.0.0") from exc
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with Client(self.url) as client:
                    result = await client.list_tools()
        except TimeoutError as exc:
            raise PhmDiagnosisMcpTimeout("list_tools") from exc
        except Exception as exc:
            raise PhmDiagnosisMcpUnavailable("list_tools", str(exc)) from exc
        return {str(tool.name) for tool in result.tools}

    async def verify_expected_tools(self) -> set[str]:
        actual = await self.list_tools()
        missing = EXPECTED_TOOLS - actual
        if missing:
            raise PhmDiagnosisMcpError("PHM_DIAGNOSIS_MCP_TOOLS_MISSING", f"PHM Diagnosis MCP 缺少工具: {sorted(missing)}")
        return actual

    async def ready(self, ready_url: str) -> dict[str, Any]:
        if self.http_client is None:
            async with httpx.AsyncClient() as client:
                response = await client.get(ready_url, timeout=min(self.timeout_seconds, 10.0))
        else:
            response = await self.http_client.get(ready_url, timeout=min(self.timeout_seconds, 10.0))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise PhmDiagnosisMcpError("PHM_DIAGNOSIS_MCP_READY_INVALID", "PHM Diagnosis MCP /ready 返回格式异常")
        return payload

    async def analyze_chart(self, **kwargs: Any) -> dict[str, Any]:
        return await self.call_tool("analyze_chart", kwargs)

    async def diagnose_point(self, **kwargs: Any) -> dict[str, Any]:
        return await self.call_tool("diagnose_point", kwargs)

    async def diagnose_device(self, **kwargs: Any) -> dict[str, Any]:
        return await self.call_tool("diagnose_device", kwargs)

    async def get_model_admission(self) -> dict[str, Any]:
        return await self.call_tool("get_model_admission", {})
