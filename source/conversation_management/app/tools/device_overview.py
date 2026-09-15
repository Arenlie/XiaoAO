from __future__ import annotations

import re

_DEVICE_INFO_HINTS = (
    "设备信息",
    "设备详情",
    "设备档案",
    "设备概况",
    "设备资料",
    "资产信息",
    "资产详情",
)
_EQUIPMENT_HINT_RE = re.compile(
    r"(?:轧机|风机|鼓风机|空压机|水泵|泵|电机|减速机|压缩机|磨机|机组|设备|机器)"
)
_GENERIC_INFO_RE = re.compile(r"(?:查询|查看|看一下|看下|了解|介绍).{0,30}(?:信息|详情|档案|资料)")


def is_device_overview_query(query: str) -> bool:
    """Return True for a device-information overview request.

    This is intentionally narrower than generic industrial Q&A. It captures requests
    whose goal is to identify one real equipment asset and summarize authoritative
    asset identity + current operational status. It must not swallow health-only,
    alarm-only, diagnosis or waveform questions.
    """

    text = re.sub(r"\s+", "", str(query or ""))
    if not text:
        return False
    if any(token in text for token in _DEVICE_INFO_HINTS):
        return True
    return bool(_EQUIPMENT_HINT_RE.search(text) and _GENERIC_INFO_RE.search(text))


def device_overview_synthesis_instruction() -> str:
    return (
        "本轮是设备信息总览。最终回答固定分成两部分：\n"
        "1. 资产与结构信息：先展示 Asset MCP 返回的真实设备编码、设备名称、设备类型、"
        "所属公司/厂区/事业部/车间/产线等数据库中实际存在的层级字段和 space_path；"
        "不得根据名称猜层级。设备内部结构、说明书、"
        "部件组成等资料只有实际检索到对应文档时才补充并逐条引用；未取得时说明尚无对应资料。\n"
        "2. 当前运行状态：展示 PHM 健康度查询返回的最新设备健康度，以及 PHM 综合报警查询"
        "返回的报警事实。若某个工具失败或无数据，明确写‘未取得/暂无数据’，不要补造。"
    )
