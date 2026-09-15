from __future__ import annotations

from app.config import Settings
from app.tools.contracts import ToolDescriptor, ToolProviderType
from app.asset_query_contract import TOOL_ID as PHM_ASSET_QUERY_COLLECTION_TOOL_ID

PHM_ASSET_QUERY_EQUIPMENT_INFO_TOOL_ID = "mcp.phm_asset.query_equipment_info"
PHM_ASSET_QUERY_SPACE_TREE_TOOL_ID = "mcp.phm_asset.query_space_tree"
PHM_ASSET_QUERY_SPACE_CHILDREN_TOOL_ID = "mcp.phm_asset.query_space_children"
PHM_ASSET_QUERY_DEVICES_TOOL_ID = "mcp.phm_asset.query_devices"
PHM_ASSET_QUERY_SCOPE_COLLECTION_TOOL_ID = "mcp.phm_asset.query_scope_collection"
PHM_ASSET_QUERY_POINTS_TOOL_ID = "mcp.phm_asset.query_points"

# resolve_entity is intentionally not exposed to Supervisor Function Calling: every
# graph passes through the unified Asset-MCP-backed entity layer before planning.
# Keeping it outside the planner prevents duplicate or skipped entity decisions.
PHM_ASSET_TOOL_IDS = {
    PHM_ASSET_QUERY_COLLECTION_TOOL_ID,
    PHM_ASSET_QUERY_EQUIPMENT_INFO_TOOL_ID,
    PHM_ASSET_QUERY_SPACE_TREE_TOOL_ID,
    PHM_ASSET_QUERY_SPACE_CHILDREN_TOOL_ID,
    PHM_ASSET_QUERY_DEVICES_TOOL_ID,
    PHM_ASSET_QUERY_SCOPE_COLLECTION_TOOL_ID,
    PHM_ASSET_QUERY_POINTS_TOOL_ID,
}


def _metadata(server_tool_name: str) -> dict:
    return {
        "config_profile": "phm_asset",
        "server_tool_name": server_tool_name,
        "transport": "streamable_http",
        "persist_full_output": True,
        "mcp_contract_version": "1.0.0",
    }


def _output_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "status": {"type": "string"},
            "truncated": {"type": "boolean"},
        },
        "required": ["success"],
        "additionalProperties": True,
    }


def phm_asset_tool_descriptors(settings: Settings) -> list[ToolDescriptor]:
    enabled = bool(settings.phm_asset_mcp_enabled and settings.phm_asset_mcp_url)
    timeout = settings.phm_asset_mcp_timeout_seconds
    common = {
        "objective": {
            "type": "string",
            "description": "本次资产结构查询的具体目标",
        },
        "required_entity_level": {
            "type": "string",
            "enum": ["area", "equipment"],
            "description": "后端用于建立实体依赖；不要自己填写 space_id/equip_no。",
        },
    }
    return [
        ToolDescriptor(
            tool_id=PHM_ASSET_QUERY_COLLECTION_TOOL_ID, display_name="设备数量、清单与分类查询",
            provider_type=ToolProviderType.MCP,
            description="按主控已理解的类别或正式设备名称条件统一查询数量、清单和分类，追问时复用同一设备集合。真实范围和历史集合由后端注入。",
            input_schema={"type": "object", "properties": {"objective": {"type": "string"}},
                          "required": ["objective"], "additionalProperties": False},
            output_schema=_output_schema(),
            enabled=enabled and settings.phm_asset_unified_query_enabled,
            timeout_seconds=timeout, metadata=_metadata("query_asset_collection"),
        ),
        ToolDescriptor(
            tool_id=PHM_ASSET_QUERY_EQUIPMENT_INFO_TOOL_ID,
            display_name="设备资产详情查询",
            provider_type=ToolProviderType.MCP,
            description=(
                "按已经解析出的真实设备编码查询设备资产详情，包括设备名称、类型、所属空间、"
                "space_path 以及 PostgreSQL catalog 中的层级 metadata。适合‘查询1#鼓风机的设备信息’"
                "这类设备总览的资产事实阶段。后端强制注入真实 equip_no，模型不得猜编码。"
            ),
            input_schema={
                "type": "object",
                "properties": {**common},
                "required": ["objective", "required_entity_level"],
                "additionalProperties": False,
            },
            output_schema=_output_schema(),
            enabled=enabled,
            timeout_seconds=timeout,
            metadata=_metadata("query_equipment_info"),
        ),
        ToolDescriptor(
            tool_id=PHM_ASSET_QUERY_SPACE_TREE_TOOL_ID,
            display_name="区域层级查询",
            provider_type=ToolProviderType.MCP,
            description=(
                "查询某真实区域/事业部/车间/产线下的完整空间层级树。适合“下属产线结构是什么样”"
                "“完整组织/空间结构”“树状结构”等问题。后端从实体解析结果注入真实 root_space_id；"
                "树节点由 PostgreSQL 确定性返回，禁止模型自行补节点或删节点。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **common,
                    "max_depth": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
                    "include_devices": {"type": "boolean", "default": False},
                    "include_points": {"type": "boolean", "default": False},
                },
                "required": ["objective", "required_entity_level"],
                "additionalProperties": False,
            },
            output_schema=_output_schema(),
            enabled=enabled,
            timeout_seconds=timeout,
            metadata=_metadata("query_space_tree"),
        ),
        ToolDescriptor(
            tool_id=PHM_ASSET_QUERY_SPACE_CHILDREN_TOOL_ID,
            display_name="下级区域查询",
            provider_type=ToolProviderType.MCP,
            description=(
                "查询区域的直接下级或指定类型的全部后代，例如“有哪些车间”“有哪些产线”“下一级有什么”。"
                "后端注入真实 space_id。若用户问某类型的所有下属（如事业部有哪些产线），可 recursive=true。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **common,
                    "child_type": {"type": "string"},
                    "recursive": {"type": "boolean", "default": False},
                },
                "required": ["objective", "required_entity_level"],
                "additionalProperties": False,
            },
            output_schema=_output_schema(),
            enabled=enabled,
            timeout_seconds=timeout,
            metadata=_metadata("query_space_children"),
        ),
        ToolDescriptor(
            tool_id=PHM_ASSET_QUERY_DEVICES_TOOL_ID,
            display_name="区域设备查询",
            provider_type=ToolProviderType.MCP,
            description=(
                "查询区域及其下属空间中的真实设备，可用 keyword 过滤“轧机/风机/水泵”等设备名称或类型。"
                "适合“第一炼钢事业部有哪些轧机”“这个车间有哪些设备”。后端注入真实 space_id。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **common,
                    "recursive": {"type": "boolean", "default": True},
                    "keyword": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 1000},
                },
                "required": ["objective", "required_entity_level"],
                "additionalProperties": False,
            },
            output_schema=_output_schema(),
            enabled=enabled,
            timeout_seconds=timeout,
            metadata=_metadata("query_devices"),
        ),
        ToolDescriptor(
            tool_id=PHM_ASSET_QUERY_SCOPE_COLLECTION_TOOL_ID,
            display_name="区域下属实体集合查询",
            provider_type=ToolProviderType.MCP,
            description=(
                "在已经解析出的真实区域下面，按结构化目标层级返回实体集合或精确数量。"
                "设备/区域分类优先使用已审核离线语义 tag_codes；模型只允许把用户术语映射到已存在标签，"
                "真实成员及数量由 PostgreSQL 决定。后端强制注入真实 root_space_id。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **common,
                    "target_entity_level": {
                        "type": "string", "enum": ["space", "equipment", "point"]
                    },
                    "target_space_type": {"type": "string"},
                    "target_equipment_type": {"type": "string"},
                    "semantic_filters": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                    "output_mode": {"type": "string", "enum": ["list", "count"], "default": "list"},
                    "recursive": {"type": "boolean", "default": True},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100000, "default": 50000},
                },
                "required": ["objective", "required_entity_level", "target_entity_level"],
                "additionalProperties": False,
            },
            output_schema=_output_schema(),
            enabled=enabled,
            timeout_seconds=max(timeout, 120.0),
            metadata=_metadata("query_scope_collection"),
        ),
        ToolDescriptor(
            tool_id=PHM_ASSET_QUERY_POINTS_TOOL_ID,
            display_name="设备测点查询",
            provider_type=ToolProviderType.MCP,
            description=(
                "查询某真实设备下的测点，支持 vibration_acceleration/vibration/temperature 和关键词过滤。"
                "适合“13号轧机有哪些振动测点”“这个设备有哪些温度测点”。后端注入真实 equip_no。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **common,
                    "point_type": {
                        "type": "string",
                        "enum": ["vibration_acceleration", "vibration", "temperature"],
                    },
                    "keyword": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 1000},
                },
                "required": ["objective", "required_entity_level"],
                "additionalProperties": False,
            },
            output_schema=_output_schema(),
            enabled=enabled,
            timeout_seconds=timeout,
            metadata=_metadata("query_points"),
        ),
    ]
