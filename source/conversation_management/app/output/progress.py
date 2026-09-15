"""Professional customer wording in existing presentation fields only.

Protocol identifiers, status values, entity codes and business evidence are not
renamed. No parallel *_cn/*_original fields are added to the event contract.
"""
from __future__ import annotations

import re

NAMES = {
    "Asset MCP：提前检索实体": "资产查询：提前检索设备与区域",
    "Asset MCP：实体检索": "资产查询：设备、测点与区域检索",
    "ReAct 决策": "查询与分析决策", "ReAct 执行": "执行查询与分析",
    "主控大模型：ReAct 下一步决策": "主控大模型：下一步处理决策",
    "PHM 空间树查询": "区域层级查询", "PHM 空间下级查询": "下级区域查询",
    "PHM 波形查询": "振动波形查询",
    "PHM 振动标量特征提取": "振动特征指标提取",
    "PHM 转速/1X 特征提取": "转速及一倍转频特征提取",
    "PHM 诊断模型准入状态": "诊断服务可用性检查",
    "设备结构 RAG 状态": "设备结构资料接入状态",
    "Embedding 模型：生成语义向量": "语义检索准备",
    "Reranker 模型：候选结果精排": "候选结果相关性排序",
    "Asset 语义大模型：解析资产查询语义": "资产查询意图解析",
    "报警 SQL 规划大模型：调用大模型生成 SQL": "报警查询条件生成",
    "Diagnosis 大模型：调用诊断大模型": "综合诊断分析",
}
DESCRIPTIONS = {
    "读取本轮所需的注册表、历史上下文和运行状态": "读取相关对话和已确认的查询对象。",
    "提取目标、范围、证据、操作、时间和资产语义，并判断是否复用可选 Recipe": "明确查询目标、对象范围和时间要求，确定适用的处理流程。",
    "父步骤墙钟时间；子步骤可能重叠，不应相加。候选就绪可提前进入选择": "部分步骤同时进行；候选对象就绪后及时返回确认。各步骤耗时不应直接相加。",
    "独立提取本轮根对象，与主控业务路由并行": "识别本轮查询对象，与问题理解和处理方案分析同步进行。",
    "按轻量模型提取的真实查询范围检索；仅数据库候选可用于选择": "根据已识别的范围提前检索，返回平台中实际登记的候选对象。",
    "提取业务目标和结构化语义；可与轻量对象识别及实体检索重叠": "明确查询要求和处理方式，与查询对象识别同步进行。",
    "调用 Asset MCP 完成语义理解、Embedding、PostgreSQL 候选召回、Reranker 精排和实体决策": "根据名称、编号和上下文检索设备、测点与区域，对候选结果进行相关性排序和身份确认。",
    "依据目标、证据契约和当前观察选择一个最有价值的下一步": "根据当前目标和已有结果，确定下一步查询或分析内容。",
    "执行当前选中的 MCP、智能体或稳定 Recipe 步骤": "按当前方案执行数据查询、特征计算或诊断分析。",
    "业务完成后检索相关资料；有独立时间预算，失败不影响已取得的业务事实": "检索相关企业资料和历史案例，为已有查询结果补充参考依据。",
    "实时输出模型提供的推理和正式回答": "综合已有信息生成分析说明和正式回答，内容陆续显示。",
}
TERMS = {
    "PHM Asset MCP": "资产查询服务", "PHM Data MCP": "设备数据服务",
    "PHM Feature MCP": "特征分析服务", "PHM Diagnosis MCP": "专业诊断服务",
    "Asset MCP": "资产查询服务", "Data MCP": "设备数据服务",
    "Feature MCP": "特征分析服务", "Diagnosis MCP": "专业诊断服务",
    "Embedding 模型": "语义匹配", "Reranker 模型": "相关性排序",
    "Asset 语义大模型": "资产查询分析", "Diagnosis 大模型": "诊断分析",
    "报警 SQL 规划大模型": "报警查询分析", "MongoDB 数据库": "监测数据查询",
    "MySQL 报警数据库": "报警记录查询", "PostgreSQL": "数据查询",
    "Embedding": "语义匹配", "Reranker": "相关性排序", "Recipe": "处理流程",
    "ReAct": "查询与分析", "RAG": "资料检索", "MCP": "服务", "SQL": "查询条件",
    "LLM": "大模型", "OCR": "图中文字识别",
}

TOOL_NAMES = {
    "resolve_entity": "设备、测点与区域检索",
    "query_equipment_info": "设备资产详情查询",
    "query_space_tree": "区域层级查询", "query_space_children": "下级区域查询",
    "query_devices": "区域设备查询", "query_scope_collection": "区域下属实体集合查询",
    "query_points": "设备测点查询",
    "query_active_sensor_faults": "传感器当前故障查询",
    "query_sensor_fault_history": "传感器历史故障查询",
    "query_offline_sensors": "离线传感器查询",
    "query_sensor_monitoring_status": "传感器监测与在线状态查询",
    "query_sensor_status_overview": "传感器状态概览查询",
    "query_monitored_sensor_points": "传感器监测测点查询",
    "get_sensor_fault_evidence": "传感器故障证据详情",
    "get_waveform": "振动波形查询", "get_feature_trend": "特征趋势查询",
    "get_temperature_trend": "温度趋势查询", "check_data_availability": "数据可用性检查",
    "get_data_snapshot": "数据快照查询", "get_device_data": "整设备全部测点数据查询",
    "query_health_score": "健康度查询", "query_alarm_records": "综合报警查询",
    "diagnose_point": "单测点综合诊断", "diagnose_device": "整设备全部测点综合诊断",
    "analyze_chart": "单图谱诊断分析", "get_model_admission": "诊断服务可用性检查",
    "extract_vibration_features": "振动特征指标提取", "extract_rotational_speed_feature": "转速及一倍转频特征提取",
}


def professional_text(value):
    text = str(value or "")
    root = re.fullmatch(r"执行\s+([a-z][a-z0-9_.]*)", text)
    if root:
        return TOOL_NAMES.get(root.group(1), "执行本项查询或分析")
    if re.search(r"(?:中执行|工具)\s*[A-Za-z_][A-Za-z0-9_.]*", text):
        return "执行本项查询或分析。"
    text = NAMES.get(text, DESCRIPTIONS.get(text, text))
    for old, new in TERMS.items():
        text = text.replace(old, new)
    text = re.sub(r"\bPHM\s*", "", text)
    return re.sub(r"(?<=：)[A-Za-z_][A-Za-z0-9_]*$", "执行本项处理", text)


def progress_payload(value, event=""):
    """Replace existing human-readable values; leave every protocol key intact."""
    result = dict(value or {})
    for key in ("name_cn", "description_cn", "display_name", "description", "display_message", "public_message"):
        if isinstance(result.get(key), str):
            result[key] = professional_text(result[key])
    for key in ("execution_trace", "trace"):
        if isinstance(result.get(key), dict) and isinstance(result[key].get("spans"), list):
            trace = dict(result[key])
            trace["spans"] = [progress_span(row) for row in trace["spans"]]
            result[key] = trace
    return result


def progress_span(row):
    if not isinstance(row, dict):
        return row
    result = dict(row)
    if "name" in result:
        text = professional_text(result["name"])
        # Instrumented public methods formerly fell back to their Python names.
        text = re.sub(r"(?<=：)[A-Za-z_][A-Za-z0-9_]*$", "执行本项处理", text)
        result["name"] = text
    if "description" in result:
        text = str(result["description"] or "")
        if re.search(r"(?:中执行|工具)\s*[A-Za-z_][A-Za-z0-9_.]*", text):
            text = "执行本项查询或分析。"
        result["description"] = professional_text(text)
    return result
