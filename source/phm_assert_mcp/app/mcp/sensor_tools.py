"""Additive sensor tools; keep existing asset tool names and contracts intact."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import ValidationError

from app.providers.sensor_agent import SensorError
from app.schemas.sensor import SensorQuery, MonitoredPointsQuery

SENSOR_TOOLS = {
    "query_active_sensor_faults": ("active", "查询当前正式传感器故障，支持设备、逻辑测点、故障中文名称或真实规则编码；核对监测范围，不把空记录当作未监测或在线。"),
    "query_sensor_fault_history": ("history", "查询传感器已结束故障，支持故障类型、状态和时间范围；按服务配置读取并核验实际返回范围，保留重复编号的历史记录，不把展示数量当成全部历史。"),
    "query_offline_sensors": ("offline", "查询离线传感器，以传感器自检服务离线列表为准；指定测点时同时区分未监测、离线、在线、无法确认。"),
    "query_sensor_monitoring_status": ("monitoring", "查询设备或测点的传感器在线状态及是否纳入自检监测；设备按真实测点核对，部分在线不能等同整台设备在线；在线不等于设备运行。"),
    "query_sensor_status_overview": ("overview", "并行查询当前正式传感器故障和离线快照，返回监测范围、故障记录数与逻辑测点数；支持指定故障类型。"),
}
SENSOR_TOOL_NAMES = (*SENSOR_TOOLS, "get_sensor_fault_evidence", "query_monitored_sensor_points")


def invalid_query(exc):
    result = SensorError("INVALID_QUERY_PARAMETER", "传感器查询参数不合法，请检查设备、测点和筛选条件。").payload()
    # Pydantic validator contexts can contain ValueError objects, which are not
    # JSON serializable and would destroy the structured MCP error response.
    result["error"]["details"] = exc.errors(include_input=False, include_context=False, include_url=False)
    return result


def register_sensor_tools(mcp, traced):
    def make_query(kind):
        async def query(
            equip_num: str | None = None,
            point_num: str | None = None,
            fault_type: str | None = None,
            fault_status: Literal["PENDING_CONFIRMATION", "PENDING_REPAIR", "REPAIR_COMPLETED", "AUTO_RECOVERED", "DATA_INTERRUPTED"] | None = None,
            start_time_from: str | None = None,
            start_time_to: str | None = None,
            end_time_from: str | None = None,
            end_time_to: str | None = None,
            limit: int = 50,
            include_analysis: bool = True,
            refresh: bool = False,
            request_id: str | None = None,
        ) -> dict[str, Any]:
            runtime = mcp.runtime
            async def operation():
                try:
                    if kind in {"offline", "monitoring"} and fault_type:
                        raise SensorError("INVALID_QUERY_PARAMETER", "在线或离线查询不按故障类型筛选；请使用传感器故障查询。")
                    q = SensorQuery(equip_num=equip_num, point_num=point_num, fault_type=fault_type, fault_status=fault_status,
                        start_time_from=start_time_from, start_time_to=start_time_to, end_time_from=end_time_from,
                        end_time_to=end_time_to, limit=limit, include_analysis=include_analysis, refresh=refresh)
                    return await runtime.sensors.query(kind, q)
                except ValidationError as exc:
                    return invalid_query(exc)
                except SensorError as exc:
                    return exc.payload()
            return await traced(runtime, next(n for n, (k, _) in SENSOR_TOOLS.items() if k == kind), operation)
        return query
    for name, (kind, description) in SENSOR_TOOLS.items():
        mcp.tool(name=name, description=description)(make_query(kind))

    @mcp.tool(name="query_monitored_sensor_points", description="查询已注册的逻辑测点、在线/离线/暂停状态及波形和特征参数配置。按服务配置读取（默认请求15000条）并核对实际总数；截断不代表未监测。编码精确匹配已登记的物理测点/参数别名，名称支持包含筛选。identity_tokens 仅用于精确核验编码及设备档案对应关系，不提供实时状态，不能与清单筛选混用。")
    async def monitored_points(equip_num: str | None = None, equip_name: str | None = None,
            point_num: str | None = None, point_name: str | None = None,
            monitor_status: str | None = None, waveform_enabled: bool | None = None,
            feature_kind: str | None = None, limit: int = 1000, refresh: bool = False,
            request_id: str | None = None, identity_tokens: list[str] | None = None) -> dict[str, Any]:
        runtime = mcp.runtime
        async def operation():
            try:
                if identity_tokens is not None:
                    if any(x is not None for x in (equip_num, equip_name, point_num, point_name, monitor_status, waveform_enabled, feature_kind)):
                        raise SensorError("INVALID_QUERY_PARAMETER", "编码核验不能与清单筛选混用。")
                    return await runtime.sensors.resolve_identity(identity_tokens, refresh=refresh)
                q = MonitoredPointsQuery(equip_num=equip_num, equip_name=equip_name, point_num=point_num,
                    point_name=point_name, monitor_status=monitor_status, waveform_enabled=waveform_enabled,
                    feature_kind=feature_kind, limit=limit, refresh=refresh)
                return await runtime.sensors.monitored_points(q)
            except ValidationError as exc:
                return invalid_query(exc)
            except SensorError as exc:
                return exc.payload()
        return await traced(runtime, "query_monitored_sensor_points", operation)

    @mcp.tool(name="get_sensor_fault_evidence", description="只在明确需要某条正式传感器故障的详细依据时读取；设备编码及故障编号必须来自真实实体和前序故障结果，禁止批量逐条读取。")
    async def evidence(fault_id: str, equip_num: str, point_num: str | None = None, request_id: str | None = None, refresh: bool = False) -> dict[str, Any]:
        runtime = mcp.runtime
        async def operation():
            try:
                q = SensorQuery(equip_num=equip_num, point_num=point_num)
                if not fault_id.strip() or len(fault_id) > 256:
                    raise SensorError("INVALID_QUERY_PARAMETER", "故障编号不能为空或超过长度限制。")
                return await runtime.sensors.fault_evidence(fault_id.strip(), q.equip_num, q.point_num, refresh=refresh)
            except ValidationError as exc:
                return invalid_query(exc)
            except SensorError as exc:
                return exc.payload()
        return await traced(runtime, "get_sensor_fault_evidence", operation)
