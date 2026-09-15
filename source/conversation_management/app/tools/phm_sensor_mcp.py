"""Sensor tool descriptions hosted on the existing Asset MCP connection."""
from app.tools.contracts import ToolDescriptor, ToolProviderType

SENSOR_OPERATIONS = {
    "points": ("query_monitored_sensor_points", "传感器监测点与配置清单", "查询已注册的全部/指定设备监测逻辑测点、总数、在线或暂停清单、温度/偏置/速度参数和波形监听能力；总注册数与本次返回数分开，来源完整性以接口实际返回数量、总数和截断标记为准；明细展示上限不等于上游检索范围。"),
    "active": ("query_active_sensor_faults", "当前传感器故障", "读取当前正式传感器故障，可按偏置电压异常、温度异常、数据卡死等具体故障类型筛选。"),
    "history": ("query_sensor_fault_history", "历史传感器故障", "读取已结束的传感器故障，按本轮结构化时间范围筛选；历史完整性以接口实际覆盖范围和截断标记为准，不根据明细展示上限推断历史缺失。"),
    "offline": ("query_offline_sensors", "离线传感器查询", "查询离线传感器和监测范围，未监测与离线不同。"),
    "monitoring": ("query_sensor_monitoring_status", "传感器在线与监测范围", "用于设备或测点是否在线、是否纳入监测、通讯/采样状态查询。设备按实际测点分别核对；在线不等于设备运行，离线不等于停机。"),
    "overview": ("query_sensor_status_overview", "传感器状态总览", "并行读取当前传感器故障和离线信息，分别统计故障记录、逻辑测点、在线、离线和未监测。"),
}
SENSOR_TOOL_BY_OPERATION = {op: "mcp.phm_asset."+spec[0] for op, spec in SENSOR_OPERATIONS.items()}
SENSOR_EVIDENCE_TOOL_ID = "mcp.phm_asset.get_sensor_fault_evidence"
PHM_SENSOR_TOOL_IDS = set(SENSOR_TOOL_BY_OPERATION.values()) | {SENSOR_EVIDENCE_TOOL_ID}


def phm_sensor_tool_descriptors(settings):
    enabled = bool(settings.phm_asset_mcp_enabled and settings.phm_asset_mcp_url and getattr(settings, "phm_sensor_tools_enabled", True))
    common = {
        "objective": {"type": "string", "description": "本轮传感器查询目标"},
        "scope": {"type": "string", "enum": ["global", "equipment", "point"], "description": "全局、已解析设备或已解析测点；不能把指定设备请求降为全局"},
        "required_entity_level": {"type": "string", "enum": ["none", "equipment", "point"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
        "refresh": {"type": "boolean", "default": False},
    }
    descriptors = []
    for op, (name, title, description) in SENSOR_OPERATIONS.items():
        props = dict(common)
        if op == "points":
            props.update(monitor_status={"type": "string", "enum": ["ONLINE", "OFFLINE", "SUSPENDED"], "description": "精确原始状态筛选；OFFLINE与SUSPENDED分别保留，无筛选不填"},
                         waveform_enabled={"type": "boolean", "description": "是否已纳入振动波形监听，必须保留false条件"},
                         feature_kind={"type": "string", "enum": ["bias", "velocity", "temperature"], "description": "偏置电压/速度有效值/温度参数配置筛选；不表示存在对应故障"},
                         limit={"type": "integer", "minimum": 1, "maximum": 1000, "default": 50})
        if op in {"active", "history", "overview"}:
            props.update(fault_type={"type": "string", "description": "用户指定的故障中文名称或规则编码，必须保留；不填表示全部类型。可承接用户明确的上下文指代。"},
                         include_analysis={"type": "boolean", "default": True})
        if op == "history":
            props["time_field"] = {"type": "string", "enum": ["start_time", "end_time"], "default": "start_time"}
        descriptors.append(ToolDescriptor(tool_id=SENSOR_TOOL_BY_OPERATION[op], display_name=title, provider_type=ToolProviderType.MCP,
            description=description+" 数据来自传感器自检 HTTP 服务，不是机械故障诊断或综合报警。真实设备/测点编码由后端统一实体层注入，不得猜测、拼接A/T/001/000后缀。必须区分未监测、查询失败和无已发布故障；按返回范围说明，不将空列表解释为在线。",
            input_schema={"type": "object", "properties": props, "required": ["objective", "scope"], "additionalProperties": False},
            output_schema={"type": "object", "properties": {"success": {"type": "boolean"}}, "additionalProperties": True},
            enabled=enabled, timeout_seconds=settings.phm_asset_mcp_timeout_seconds,
            metadata={"config_profile": "phm_asset", "server_tool_name": name, "transport": "streamable_http", "persist_full_output": True, "mcp_contract_version": "1.5.1"}))
    descriptors.append(ToolDescriptor(tool_id=SENSOR_EVIDENCE_TOOL_ID, display_name="传感器故障证据详情", provider_type=ToolProviderType.MCP,
        description="明确需要某条传感器正式故障的详细依据时使用。先查询故障列表，fault_id只可来自真实列表或用户明确给出的编号；设备身份由后端注入。不能默认对全部故障逐条取证据。",
        input_schema={"type": "object", "properties": {"objective": {"type": "string"}, "fault_id": {"type": "string"}, "refresh": {"type": "boolean", "default": False}}, "required": ["objective", "fault_id"], "additionalProperties": False},
        output_schema={"type": "object", "additionalProperties": True}, enabled=enabled, timeout_seconds=settings.phm_asset_mcp_timeout_seconds,
        metadata={"config_profile": "phm_asset", "server_tool_name": "get_sensor_fault_evidence", "transport": "streamable_http", "persist_full_output": True, "mcp_contract_version": "1.5.1"}))
    return descriptors
