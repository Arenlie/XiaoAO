from __future__ import annotations

from app.config import Settings
from app.tools.contracts import ToolDescriptor, ToolProviderType

PHM_FEATURE_EXTRACT_VIBRATION_TOOL_ID = "mcp.phm_feature.extract_vibration_features"
PHM_FEATURE_EXTRACT_RPM_TOOL_ID = "mcp.phm_feature.extract_rotational_speed_feature"

PHM_FEATURE_TOOL_IDS = {
    PHM_FEATURE_EXTRACT_VIBRATION_TOOL_ID,
    PHM_FEATURE_EXTRACT_RPM_TOOL_ID,
}


def _metadata(server_tool_name: str) -> dict:
    return {
        "config_profile": "phm_feature",
        "server_tool_name": server_tool_name,
        "transport": "streamable_http",
        "mcp_contract_version": "1.0.0",
        "persist_full_output": True,
    }


def phm_feature_tool_descriptors(settings: Settings) -> list[ToolDescriptor]:
    enabled = bool(settings.phm_feature_mcp_enabled and settings.phm_feature_mcp_url)
    return [
        ToolDescriptor(
            tool_id=PHM_FEATURE_EXTRACT_VIBRATION_TOOL_ID,
            display_name="振动特征指标提取",
            provider_type=ToolProviderType.MCP,
            description=(
                "从已取得的真实振动波形提取结构化标量特征，包含 RMS、峰值、峭度、峰值因子、"
                "频域统计、包络域统计和可选自定义频带能量。系统会先通过 PHM Data MCP 获取波形，"
                "再把 little-endian float32 Base64 原样注入；主控不得自行构造、解码或改写波形。"
                "适合用户明确要求振动特征、RMS/峭度/峰值因子/频带能量，或其他后续模型需要标量特征时使用。"
                "不用于画完整频谱，也不负责故障类型诊断。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "objective": {"type": "string", "description": "本次特征提取目标"},
                    "required_entity_level": {
                        "type": "string",
                        "enum": ["point"],
                        "default": "point",
                    },
                    "feature_set": {
                        "type": "string",
                        "enum": ["basic", "standard", "bearing", "full"],
                        "default": "standard",
                    },
                    "signal_type": {
                        "type": "string",
                        "enum": ["acceleration", "velocity", "displacement"],
                        "default": "acceleration",
                        "description": "只有已知波形物理量时覆盖；默认按加速度波形处理",
                    },
                    "unit": {
                        "type": "string",
                        "description": "已知输入波形单位时透传；服务不会自动换算单位",
                    },
                    "detrend": {"type": "boolean", "default": True},
                    "frequency_bands": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "low_hz": {"type": "number", "minimum": 0},
                                "high_hz": {"type": "number", "exclusiveMinimum": 0},
                            },
                            "required": ["name", "low_hz", "high_hz"],
                            "additionalProperties": False,
                        },
                    },
                    "envelope_band": {
                        "type": "object",
                        "properties": {
                            "low_hz": {"type": "number", "exclusiveMinimum": 0},
                            "high_hz": {"type": "number", "exclusiveMinimum": 0},
                            "filter_order": {"type": "integer", "minimum": 1, "maximum": 10},
                        },
                        "required": ["low_hz", "high_hz"],
                        "additionalProperties": False,
                    },
                },
                "required": ["objective"],
                "additionalProperties": False,
            },
            output_schema={"type": "object", "additionalProperties": True},
            enabled=enabled,
            timeout_seconds=settings.phm_feature_mcp_feature_timeout_seconds,
            metadata=_metadata("extract_vibration_features"),
        ),
        ToolDescriptor(
            tool_id=PHM_FEATURE_EXTRACT_RPM_TOOL_ID,
            display_name="转速及一倍转频特征提取",
            provider_type=ToolProviderType.MCP,
            description=(
                "从已取得的真实振动波形识别轴旋转频率、1X、RPM、倍频证据和置信度。"
                "系统会先通过 PHM Data MCP 获取波形并原样传递 Base64。适合“转速是多少”“1X是多少”"
                "以及阶比谱/解调阶比谱等后续专业分析需要可靠 RPM 的场景。supported=false 表示"
                "当前波形证据不足，不是系统调用失败；不得凭空补一个转速。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "objective": {"type": "string", "description": "本次转速识别目标"},
                    "required_entity_level": {
                        "type": "string",
                        "enum": ["point"],
                        "default": "point",
                    },
                    "signal_type": {
                        "type": "string",
                        "enum": ["acceleration", "velocity"],
                        "default": "acceleration",
                        "description": "真实波形类型；默认按加速度输入并允许服务内部派生速度",
                    },
                    "use_llm_judge": {
                        "type": "boolean",
                        "description": "是否请求 MCP 在既有候选之间使用 LLM 仲裁；省略则跟随 MCP 服务端配置",
                    },
                },
                "required": ["objective"],
                "additionalProperties": False,
            },
            output_schema={"type": "object", "additionalProperties": True},
            enabled=enabled,
            timeout_seconds=settings.phm_feature_mcp_rpm_timeout_seconds,
            metadata=_metadata("extract_rotational_speed_feature"),
        ),
    ]
