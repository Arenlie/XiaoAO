from __future__ import annotations

from app.config import Settings
from app.tools.contracts import ToolDescriptor, ToolProviderType

PHM_GET_WAVEFORM_TOOL_ID = "mcp.phm_data.get_waveform"
PHM_GET_FEATURE_TREND_TOOL_ID = "mcp.phm_data.get_feature_trend"
PHM_GET_TEMPERATURE_TREND_TOOL_ID = "mcp.phm_data.get_temperature_trend"
PHM_CHECK_DATA_AVAILABILITY_TOOL_ID = "mcp.phm_data.check_data_availability"
PHM_GET_DATA_SNAPSHOT_TOOL_ID = "mcp.phm_data.get_data_snapshot"
PHM_GET_DEVICE_DATA_TOOL_ID = "mcp.phm_data.get_device_data"
PHM_QUERY_ALARM_RECORDS_TOOL_ID = "mcp.phm_data.query_alarm_records"
PHM_QUERY_HEALTH_SCORE_TOOL_ID = "mcp.phm_data.query_health_score"

PHM_DATA_TOOL_IDS = {
    PHM_GET_WAVEFORM_TOOL_ID,
    PHM_GET_FEATURE_TREND_TOOL_ID,
    PHM_GET_TEMPERATURE_TREND_TOOL_ID,
    PHM_CHECK_DATA_AVAILABILITY_TOOL_ID,
    PHM_GET_DATA_SNAPSHOT_TOOL_ID,
    PHM_GET_DEVICE_DATA_TOOL_ID,
    PHM_QUERY_ALARM_RECORDS_TOOL_ID,
    PHM_QUERY_HEALTH_SCORE_TOOL_ID,
}

PHM_DATA_BINARY_TOOL_IDS = {
    PHM_GET_WAVEFORM_TOOL_ID,
    PHM_GET_FEATURE_TREND_TOOL_ID,
    PHM_GET_TEMPERATURE_TREND_TOOL_ID,
    PHM_GET_DATA_SNAPSHOT_TOOL_ID,
    PHM_GET_DEVICE_DATA_TOOL_ID,
}


def _base_metadata(server_tool_name: str, *, contains_binary: bool = False) -> dict:
    return {
        "config_profile": "phm_data",
        "server_tool_name": server_tool_name,
        "transport": "streamable_http",
        "contains_binary_payload": contains_binary,
        "persist_full_output": False if contains_binary else True,
        "mcp_contract_version": "1.0.0",
    }


def _base_output_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "data": {},
            "decode": {"type": "object"},
            "warnings": {"type": "array", "items": {"type": "string"}},
            "error": {},
        },
        "required": ["success"],
        "additionalProperties": True,
    }


def phm_data_tool_descriptors(settings: Settings) -> list[ToolDescriptor]:
    enabled = bool(settings.phm_data_mcp_enabled and settings.phm_data_mcp_url)
    timeout = settings.phm_data_mcp_timeout_seconds
    device_timeout = getattr(settings, "phm_data_mcp_device_timeout_seconds", timeout)
    common_entity = {
        "objective": {
            "type": "string",
            "description": "本次数据查询的具体、可验证目标",
        },
        "required_entity_level": {
            "type": "string",
            "enum": ["none", "area", "equipment", "point"],
            "description": "需要的企业实体层级。后端会用已解析实体注入真实编码，不要猜测编码。",
        },
    }

    return [
        ToolDescriptor(
            tool_id=PHM_GET_WAVEFORM_TOOL_ID,
            display_name="振动波形查询",
            provider_type=ToolProviderType.MCP,
            description=(
                "查询某设备测点的振动波形。适合用户明确要求最新波形、报警时刻附近波形、"
                "时域/频谱/包络等后续算法所需原始波形。后端自动从已确认实体或诊断上下文"
                "注入 device_code 和 point_no；不要让模型猜设备/测点编码。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **common_entity,
                    "target_time": {
                        "type": "string",
                        "description": "ISO 8601 时间；nearest/latest_before 时使用",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["latest", "nearest", "latest_before"],
                        "default": "latest",
                    },
                    "search_window_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 604800,
                        "default": 86400,
                    },
                },
                "required": ["objective", "required_entity_level"],
                "additionalProperties": False,
            },
            output_schema=_base_output_schema(),
            enabled=enabled,
            timeout_seconds=timeout,
            metadata=_base_metadata("get_waveform", contains_binary=True),
        ),
        ToolDescriptor(
            tool_id=PHM_GET_FEATURE_TREND_TOOL_ID,
            display_name="特征趋势查询",
            provider_type=ToolProviderType.MCP,
            description=(
                "查询设备测点的振动/状态特征趋势，例如速度有效值、低频/高频加速度、峰值、"
                "冲击值、峭度、LR/HR 等。若 fuzzy-entity 仅返回 A/T 测点码，后端会确定性"
                "派生 A 原始点并默认查询 KPI 001~008；禁止主控自己拼编码。用户明确问"
                "温度趋势时优先使用温度趋势专用工具。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **common_entity,
                    "start_time": {"type": "string"},
                    "end_time": {"type": "string"},
                    "days": {"type": "integer", "minimum": 1, "maximum": 3650, "default": 30},
                    "kpi_ids": {"type": "array", "items": {"type": "string"}},
                    "return_mode": {
                        "type": "string",
                        "enum": ["auto", "inline", "binary"],
                        "default": "auto",
                    },
                },
                "required": ["objective", "required_entity_level"],
                "additionalProperties": False,
            },
            output_schema=_base_output_schema(),
            enabled=enabled,
            timeout_seconds=timeout,
            metadata=_base_metadata("get_feature_trend", contains_binary=True),
        ),
        ToolDescriptor(
            tool_id=PHM_GET_TEMPERATURE_TREND_TOOL_ID,
            display_name="温度趋势查询",
            provider_type=ToolProviderType.MCP,
            description=(
                "查询设备测点的温度 KPI 趋势。若 fuzzy-entity 返回 A 或 T 测点码，后端自动"
                "派生对应 T 原始点并固定 KPI=000；温度原始 T 码本身不作为数据查询结果。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **common_entity,
                    "start_time": {"type": "string"},
                    "end_time": {"type": "string"},
                    "days": {"type": "integer", "minimum": 1, "maximum": 3650, "default": 30},
                    "kpi_id": {"type": "string"},
                    "return_mode": {
                        "type": "string",
                        "enum": ["auto", "inline", "binary"],
                        "default": "auto",
                    },
                },
                "required": ["objective", "required_entity_level"],
                "additionalProperties": False,
            },
            output_schema=_base_output_schema(),
            enabled=enabled,
            timeout_seconds=timeout,
            metadata=_base_metadata("get_temperature_trend", contains_binary=True),
        ),
        ToolDescriptor(
            tool_id=PHM_CHECK_DATA_AVAILABILITY_TOOL_ID,
            display_name="数据可用性检查",
            provider_type=ToolProviderType.MCP,
            description=(
                "检查指定设备/测点是否存在波形、特征趋势以及可用 KPI。仅当后续路由确实依赖"
                "‘有没有这类数据’时调用，不要把它作为每次数据查询的固定前置步骤。"
            ),
            input_schema={
                "type": "object",
                "properties": {**common_entity},
                "required": ["objective", "required_entity_level"],
                "additionalProperties": False,
            },
            output_schema=_base_output_schema(),
            enabled=enabled,
            timeout_seconds=timeout,
            metadata=_base_metadata("check_data_availability"),
        ),
        ToolDescriptor(
            tool_id=PHM_GET_DATA_SNAPSHOT_TOOL_ID,
            display_name="数据快照查询",
            provider_type=ToolProviderType.MCP,
            description=(
                "围绕一个时间锚点一次获取最近波形和锚点前特征趋势，适合首次综合诊断或用户"
                "明确要求报警时刻附近的数据快照。返回 warning 只表示时间对齐偏差，不等同于失败。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **common_entity,
                    "target_time": {"type": "string"},
                    "kpi_ids": {"type": "array", "items": {"type": "string"}},
                    "trend_days": {"type": "integer", "minimum": 1, "maximum": 3650, "default": 30},
                    "trend_hours": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "description": "可选小时级趋势窗口；提供时优先于 trend_days。",
                    },
                    "tolerance_seconds": {"type": "integer", "minimum": 0, "default": 3600},
                    "search_window_seconds": {"type": "integer", "minimum": 1, "default": 86400},
                },
                "required": ["objective", "required_entity_level"],
                "additionalProperties": False,
            },
            output_schema=_base_output_schema(),
            enabled=enabled,
            timeout_seconds=timeout,
            metadata=_base_metadata("get_data_snapshot", contains_binary=True),
        ),
        ToolDescriptor(
            tool_id=PHM_GET_DEVICE_DATA_TOOL_ID,
            display_name="整设备全部测点数据查询",
            provider_type=ToolProviderType.MCP,
            description=(
                "输入设备编码，一次获取该设备 MongoDB 中全部可用波形测点与特征趋势测点。"
                "用于设备级综合诊断，或用户明确要求查看整台设备全部测点数据。后端只注入已确认"
                "的 device_code，不要求先下钻到单测点。返回的大波形/趋势由平台生成解码视图，"
                "原始 Base64 仍在当前 Worker 内存中原样传给 Diagnosis MCP。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **common_entity,
                    "target_time": {"type": "string", "description": "可选 ISO 8601 时间锚点"},
                    "trend_days": {"type": "integer", "minimum": 1, "maximum": 3650, "default": 30},
                    "trend_hours": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "description": "可选小时级趋势窗口；提供时优先于 trend_days。",
                    },
                    "fallback_trend_hours": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "description": "超时或响应过大时使用的小时级降级窗口。",
                    },
                    "search_window_seconds": {"type": "integer", "minimum": 1, "maximum": 604800, "default": 86400},
                },
                "required": ["objective", "required_entity_level"],
                "additionalProperties": False,
            },
            output_schema=_base_output_schema(),
            enabled=enabled,
            timeout_seconds=device_timeout,
            metadata=_base_metadata("get_device_data", contains_binary=True),
        ),
        ToolDescriptor(
            tool_id=PHM_QUERY_HEALTH_SCORE_TOOL_ID,
            display_name="健康度查询",
            provider_type=ToolProviderType.MCP,
            description=(
                "查询设备或区域健康度。设备支持当前最新健康度和指定时间范围历史健康度；"
                "区域仅支持当前实时健康度。健康分必须以该工具返回为准，不得根据报警、"
                "波形或趋势自行计算。后端会从已解析实体注入真实设备编码或 space_id，"
                "模型不得把设备名、区域名或 space_link 直接当 scope_id。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **common_entity,
                    "scope_type": {
                        "type": "string",
                        "enum": ["device", "space"],
                        "description": "设备健康度使用 device；区域健康度使用 space。",
                    },
                    "start_time": {
                        "type": "string",
                        "description": "设备历史健康度开始时间，ISO 8601；当前健康度不要传。",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "设备历史健康度结束时间，ISO 8601；当前健康度不要传。",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 100,
                        "description": "设备历史健康度最大返回数量；未明确时默认100。",
                    },
                },
                "required": ["objective", "required_entity_level", "scope_type"],
                "additionalProperties": False,
            },
            output_schema=_base_output_schema(),
            enabled=enabled,
            timeout_seconds=timeout,
            metadata=_base_metadata("query_health_score"),
        ),
        ToolDescriptor(
            tool_id=PHM_QUERY_ALARM_RECORDS_TOOL_ID,
            display_name="综合报警查询",
            provider_type=ToolProviderType.MCP,
            description=(
                "统一查询阈值、趋势、机理/诊断、AI 四类报警的明细、最近报警、时间范围统计和排行。"
                "区域查询使用已解析 space_link 前缀覆盖全部下级设备；设备类型关键词会映射为"
                "equip_name_keyword。相对时间必须由主控基于权威运行时钟换算成带时区的绝对"
                "start_time/end_time 后传入；Data MCP 不解析中文相对时间。该工具取代旧 Dify 综合报警查询。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **common_entity,
                    "alarm_types": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["threshold", "trend", "diagnosis", "ai"],
                        },
                    },
                    "time_mode": {
                        "type": "string",
                        "enum": ["default", "latest", "nearest", "latest_before", "range"],
                    },
                    "target_time": {
                        "type": "string",
                        "description": "nearest/latest_before 使用的绝对 ISO 8601 时间，必须基于运行时钟计算。",
                    },
                    "start_time": {
                        "type": "string",
                        "description": "范围开始的绝对 ISO 8601 时间，建议携带 +08:00 等时区偏移。",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "范围结束的绝对 ISO 8601 时间，建议携带 +08:00 等时区偏移。",
                    },
                    "metric": {
                        "type": "string",
                        "enum": ["detail", "count_records", "sum_occurrences", "distinct_equipment"],
                    },
                    "group_by": {
                        "type": "string",
                        "enum": [
                            "alarm_type",
                            "equipment",
                            "space",
                            "warn_level",
                            "model",
                            "deal_status",
                            "confirm_status",
                        ],
                    },
                    "model_no": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
                },
                "required": ["objective", "required_entity_level"],
                "additionalProperties": False,
            },
            output_schema=_base_output_schema(),
            enabled=enabled,
            timeout_seconds=timeout,
            metadata=_base_metadata("query_alarm_records"),
        ),
    ]
