from __future__ import annotations

from app.config import Settings
from app.tools.contracts import ToolDescriptor, ToolProviderType

COMPREHENSIVE_ALARM_TOOL_ID = "dify.comprehensive_alarm_query"


def comprehensive_alarm_tool_descriptor(settings: Settings) -> ToolDescriptor:
    configured = bool(
        settings.comprehensive_alarm_workflow_enabled
        and settings.comprehensive_alarm_workflow_base_url
        and settings.comprehensive_alarm_workflow_api_key
    )
    return ToolDescriptor(
        tool_id=COMPREHENSIVE_ALARM_TOOL_ID,
        display_name="综合报警查询",
        provider_type=ToolProviderType.DIFY_WORKFLOW,
        description=(
            "查询阈值、趋势、机理/诊断和 AI 四类报警。支持当前/历史报警、时间范围、"
            "区域及全部下级设备、指定设备/测点、设备类型关键词、报警等级、处理/确认/"
            "抑制状态、数量统计、分组和排行。区域或设备编码不明确时，应先调用模糊对应"
            "智能体；本工具由后端自动注入用户原问题、实体结果和 query_scope。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "objective": {
                    "type": "string",
                    "description": "本次报警查询的具体目标，包含报警类型、状态、时间和统计口径",
                },
                "required_entity_level": {
                    "type": "string",
                    "enum": ["none", "area", "equipment", "point"],
                    "description": (
                        "问题明确涉及区域/设备/测点时选择对应层级；全局报警查询选择 none"
                    ),
                },
                "alarm_types": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["threshold", "trend", "diagnosis", "ai"],
                    },
                    "description": "用户明确指定的报警类型；仅说报警情况时可省略，默认查询全部四类",
                },
                "query_type": {
                    "type": "string",
                    "enum": ["detail", "count", "aggregate", "ranking"],
                    "description": "明细、数量、分组统计或排行",
                },
                "alarm_state": {
                    "type": "string",
                    "enum": ["active", "ended", "all"],
                    "description": "当前持续、已结束或全部；用户未说明时省略",
                },
                "group_by": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选分组维度，如 alarm_type、equip_no、space_name、warn_level_ch",
                },
                "time_range": {
                    "type": "object",
                    "properties": {
                        "start_time": {"type": "string"},
                        "end_time": {"type": "string"},
                    },
                    "description": "可选的明确开始/结束时间；自然语言时间仍可由 Workflow 解析",
                },
                "warn_levels": {
                    "type": "array",
                    "items": {"type": ["string", "integer"]},
                },
                "deal_status": {"type": ["string", "integer"]},
                "confirm_status": {"type": ["string", "integer"]},
                "restrain_flag": {"type": ["boolean", "integer", "string"]},
                "model_no": {"type": "string"},
                "model_name": {"type": "string"},
                "sort_by": {"type": "string"},
                "sort_order": {
                    "type": "string",
                    "enum": ["asc", "desc"],
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                },
            },
            "required": ["objective", "required_entity_level"],
            "additionalProperties": True,
        },
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "message": {"type": "string"},
                "query_mode": {"type": "string"},
                "record_count": {"type": "string"},
                "result_json": {"type": "string"},
            },
        },
        requires_data_access_token=True,
        enabled=configured,
        timeout_seconds=settings.comprehensive_alarm_workflow_timeout_seconds,
        metadata={
            "config_profile": "comprehensive_alarm",
            "token_input_name": "data_access_token",
            "database_only": True,
            "workflow_contract_version": "2.0",
        },
    )
