from __future__ import annotations

from app.config import Settings
from app.tools.contracts import ToolDescriptor, ToolProviderType

PHM_DIAGNOSIS_ANALYZE_CHART_TOOL_ID = "mcp.phm_diagnosis.analyze_chart"
PHM_DIAGNOSIS_POINT_TOOL_ID = "mcp.phm_diagnosis.diagnose_point"
PHM_DIAGNOSIS_DEVICE_TOOL_ID = "mcp.phm_diagnosis.diagnose_device"
# Backward-compatible internal symbol. R29 maps the old concept of "comprehensive"
# diagnosis to the new device-level diagnose_device tool; the server no longer exposes
# comprehensive_diagnosis.
PHM_DIAGNOSIS_COMPREHENSIVE_TOOL_ID = PHM_DIAGNOSIS_DEVICE_TOOL_ID
PHM_DIAGNOSIS_MODEL_ADMISSION_TOOL_ID = "mcp.phm_diagnosis.get_model_admission"

PHM_DIAGNOSIS_TOOL_IDS = {
    PHM_DIAGNOSIS_ANALYZE_CHART_TOOL_ID,
    PHM_DIAGNOSIS_POINT_TOOL_ID,
    PHM_DIAGNOSIS_DEVICE_TOOL_ID,
    PHM_DIAGNOSIS_MODEL_ADMISSION_TOOL_ID,
}

TREND_CHART_TYPES = {"feature_trend", "temperature_trend"}
ORDER_CHART_TYPES = {"order_spectrum", "envelope_order_spectrum"}


def _metadata(server_tool_name: str) -> dict:
    return {
        "config_profile": "phm_diagnosis",
        "server_tool_name": server_tool_name,
        "transport": "streamable_http",
        "mcp_contract_version": "1.0.0",
        "persist_full_output": False,
    }


def phm_diagnosis_tool_descriptors(settings: Settings) -> list[ToolDescriptor]:
    enabled = bool(settings.phm_diagnosis_mcp_enabled and settings.phm_diagnosis_mcp_url)
    timeout = settings.phm_diagnosis_mcp_timeout_seconds
    device_timeout = getattr(settings, "phm_diagnosis_mcp_device_timeout_seconds", timeout)
    return [
        ToolDescriptor(
            tool_id=PHM_DIAGNOSIS_ANALYZE_CHART_TOOL_ID,
            display_name="单图谱诊断分析",
            provider_type=ToolProviderType.MCP,
            description=(
                "分析一个指定专业图谱。波形类图谱由当前 Worker 中的 Data MCP 原始波形注入，"
                "趋势类图谱由 Data MCP 趋势结果注入；Diagnosis MCP 自己负责 Base64 解码。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "objective": {"type": "string"},
                    "chart_type": {
                        "type": "string",
                        "enum": [
                            "time_waveform", "frequency_spectrum", "envelope_spectrum",
                            "order_spectrum", "power_spectrum", "cepstrum",
                            "envelope_order_spectrum", "feature_trend", "temperature_trend",
                        ],
                    },
                    "speed_rpm": {"type": "number", "exclusiveMinimum": 0},
                    "preprocess": {"type": "string", "enum": ["none", "demean", "detrend", "median_filter"]},
                    "median_window": {"type": "integer", "minimum": 3, "maximum": 101},
                    "max_points": {"type": "integer", "minimum": 100, "maximum": 50000},
                },
                "required": ["objective", "chart_type"],
                "additionalProperties": False,
            },
            output_schema={"type": "object", "additionalProperties": True},
            enabled=enabled,
            timeout_seconds=timeout,
            metadata=_metadata("analyze_chart"),
        ),
        ToolDescriptor(
            tool_id=PHM_DIAGNOSIS_POINT_TOOL_ID,
            display_name="单测点综合诊断",
            provider_type=ToolProviderType.MCP,
            description=(
                "对一个明确测点做综合诊断。适用于用户明确指定测点、轴承位置或要求分析单测点。"
                "平台先取得该测点波形/特征趋势/温度趋势，再将原始 Data MCP 结构原样传入。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "objective": {"type": "string"},
                    "speed_rpm": {"type": "number", "exclusiveMinimum": 0},
                    "use_model": {"type": "boolean", "default": True},
                },
                "required": ["objective"],
                "additionalProperties": False,
            },
            output_schema={"type": "object", "additionalProperties": True},
            enabled=enabled,
            timeout_seconds=timeout,
            metadata=_metadata("diagnose_point"),
        ),
        ToolDescriptor(
            tool_id=PHM_DIAGNOSIS_DEVICE_TOOL_ID,
            display_name="整设备全部测点综合诊断",
            provider_type=ToolProviderType.MCP,
            description=(
                "对整台设备全部可用测点进行综合诊断。设备级诊断默认使用该工具：平台先调用"
                "PHM Data MCP get_device_data 一次获取整设备全部测点数据，再按物理测点"
                "组装 points 并调用 diagnose_device。不要先把设备强制下钻为单测点。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "objective": {"type": "string"},
                    "speed_rpm": {"type": "number", "exclusiveMinimum": 0},
                    "use_model": {"type": "boolean", "default": True},
                },
                "required": ["objective"],
                "additionalProperties": False,
            },
            output_schema={"type": "object", "additionalProperties": True},
            enabled=enabled,
            timeout_seconds=device_timeout,
            metadata=_metadata("diagnose_device"),
        ),
        ToolDescriptor(
            tool_id=PHM_DIAGNOSIS_MODEL_ADMISSION_TOOL_ID,
            display_name="诊断服务可用性检查",
            provider_type=ToolProviderType.MCP,
            description="查询 PHM Diagnosis MCP 内部模型是否启用、健康和允许调用。",
            input_schema={
                "type": "object",
                "properties": {"objective": {"type": "string"}},
                "required": ["objective"],
                "additionalProperties": False,
            },
            output_schema={"type": "object", "additionalProperties": True},
            enabled=enabled,
            timeout_seconds=timeout,
            metadata=_metadata("get_model_admission"),
        ),
    ]
