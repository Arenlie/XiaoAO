"""Parallel queries for separately named targets; each target is independently verified."""
from __future__ import annotations

import asyncio
import unicodedata

from app.tools.contracts import ToolCallResult, ToolDescriptor
from app.tools.phm_data_context import build_phm_data_arguments
from app.tools.phm_data_decoder import decode_phm_data_for_public
from app.tools.phm_data_mcp import PHM_QUERY_ALARM_RECORDS_TOOL_ID, PHM_QUERY_HEALTH_SCORE_TOOL_ID
from app.workflows.contracts import IndependentQuery, IndependentQueries
from app.tools.phm_sensor_mcp import SENSOR_TOOL_BY_OPERATION
from app.tools.phm_sensor_context import build_sensor_arguments

MULTI_QUERY_TOOL_ID = "workflow.phm.independent_queries"


def multi_query_descriptor(settings):
    return ToolDescriptor(
        tool_id=MULTI_QUERY_TOOL_ID, provider_type="local", display_name="多个对象并行查询",
        description="对用户明确列出的多个对象分别核验实体并行查询健康度、报警、设备信息、测点或传感器故障与在线状态。传感器查询支持设备/测点及各自的故障类型。每个目标必须来自用户原文；某一目标重名或失败时保留其他目标结果，不猜选实体。",
        enabled=settings.phm_asset_mcp_enabled and settings.phm_data_mcp_enabled,
        timeout_seconds=max(settings.phm_asset_mcp_timeout_seconds, settings.phm_data_mcp_timeout_seconds) * 3,
        input_schema=IndependentQueries.model_json_schema(),
    )


class IndependentQueryHandler:
    def __init__(self, asset_client, data_client, settings):
        self.asset, self.data, self.settings = asset_client, data_client, settings
        self.slots = asyncio.Semaphore(max(1, settings.normal_max_parallel_calls))

    async def __call__(self, request, data_access_token):
        state = request.arguments.get("_verified_request_context") or {}
        raw_queries = request.arguments.get("queries") or []
        if not 2 <= len(raw_queries) <= 8:
            return ToolCallResult(tool_id=request.tool_id, status="REJECTED", error_message="请提供二到八个独立查询目标。")
        try:
            queries = [IndependentQuery.model_validate(item) for item in raw_queries]
        except ValueError:
            return ToolCallResult(tool_id=request.tool_id, status="REJECTED", error_message="查询条件不完整。")
        def norm(text):
            return "".join(unicodedata.normalize("NFKC", str(text)).casefold().split())
        evidence = [state.get("query", "")] + list(state.get("attachment_texts") or [])
        async def one(item):
            row = {"target": item.target, "query": item.query, "operation": item.operation}
            if not any(norm(item.target) in norm(text) for text in evidence):
                return {**row, "status":"REJECTED", "message":"该查询目标未在当前问题或附件中得到确认。"}
            if item.operation in {"equipment_info", "points"} and item.entity_level != "equipment":
                return {**row, "status":"NEEDS_INPUT", "message":"这项查询需要指定具体设备。"}
            sensor_op = item.operation.removeprefix("sensor_") if item.operation.startswith("sensor_") else None
            if sensor_op and (not getattr(self.settings, "phm_sensor_tools_enabled", True) or item.entity_level == "space"):
                return {**row, "status":"REJECTED", "message":"传感器并行查询需要已启用该能力，并指定具体设备或测点。"}
            if not sensor_op and item.entity_level == "point":
                return {**row, "status":"REJECTED", "message":"这项批量查询需要设备或区域范围。"}
            async with self.slots:
                try:
                    from app.services.sensor_references import code_tokens
                    if sensor_op and code_tokens(item.target):
                        from types import SimpleNamespace
                        from app.orchestration.sensor_identity import prepare_identity, validate_result
                        from app.tools.phm_sensor_mcp import phm_sensor_tool_descriptors
                        helper = SimpleNamespace(entity_resolution_layer=SimpleNamespace(client=self.asset),
                            tool_registry=SimpleNamespace(list_descriptors=lambda: phm_sensor_tool_descriptors(self.settings)))
                        intent = {"sensor_query": {"operation": sensor_op, "scope": item.entity_level,
                            "fault_type": item.fault_type, "time_field": item.time_field,
                            "monitor_status": item.monitor_status, "waveform_enabled": item.waveform_enabled,
                            "feature_kind": item.feature_kind}, "time_range": item.time_range.model_dump(mode="json")}
                        scoped = await prepare_identity(helper, {"query": item.target, "task_id": str(request.task_id)}, intent)
                        if scoped is not None:
                            if not (scoped.get("sensor_target") or {}).get("verified"):
                                return {**row, "status": "NEEDS_INPUT", "message": scoped.get("sensor_identity_error")}
                            tool = SENSOR_TOOL_BY_OPERATION[sensor_op]
                            args, missing = build_sensor_arguments(tool, scoped, {})
                            if missing:
                                return {**row, "status": "NEEDS_INPUT", "message": "；".join(missing)}
                            data = await self.asset.call_tool(tool.rsplit(".", 1)[-1], args)
                            if not validate_result(scoped, args, data) or data.get("success") is False:
                                return {**row, "status": "FAILED", "message": "该对象未取得通过核验的传感器结果。"}
                            return {**row, "status": "SUCCESS", "entity": scoped.get("resolved_entity") or {},
                                    "sensor_target": scoped["sensor_target"], "result": data}
                    resolved = await self.asset.resolve_entity(
                        query=item.target, required_entity_level=item.entity_level,
                        active_entity=state.get("active_entity") if item.use_context_reference else None,
                        conversation_context=state.get("conversation_context") if item.use_context_reference else None,
                        allow_context_reuse=item.use_context_reference,
                        request_id=f"{request.task_id}:multi",
                    )
                    if resolved.get("status") != "RESOLVED" or not resolved.get("entity"):
                        message = ("有多个匹配对象，请补充所属区域或完整名称。"
                                   if resolved.get("needs_disambiguation") else "未定位到该对象，请补充完整名称。")
                        return {**row, "status":"NEEDS_INPUT", "message":message}
                    entity = resolved["entity"]
                    if item.operation == "equipment_info":
                        data = await self.asset.query_equipment_info(equip_no=entity["equip_no"])
                    elif item.operation == "points":
                        data = await self.asset.query_points(equip_no=entity["equip_no"])
                    elif sensor_op:
                        scoped = {"query": item.query, "selected_entity": entity,
                            "business_intent": {"time_range": item.time_range.model_dump(mode="json"),
                                "sensor_query": {"operation": sensor_op, "scope": item.entity_level,
                                                 "fault_type": item.fault_type, "time_field": item.time_field,
                                                 "monitor_status": item.monitor_status,
                                                 "waveform_enabled": item.waveform_enabled, "feature_kind": item.feature_kind}}}
                        tool = SENSOR_TOOL_BY_OPERATION[sensor_op]
                        arguments, missing = build_sensor_arguments(tool, scoped, {})
                        if missing:
                            return {**row, "status":"NEEDS_INPUT", "message":"；".join(missing)}
                        data = await self.asset.call_tool(tool.rsplit(".", 1)[-1], arguments)
                        from app.orchestration.sensor_identity import validate_result
                        if not validate_result(scoped, arguments, data):
                            return {**row, "status": "FAILED", "message": "该对象未取得通过核验的传感器结果。"}
                    else:
                        tool = PHM_QUERY_HEALTH_SCORE_TOOL_ID if item.operation == "health" else PHM_QUERY_ALARM_RECORDS_TOOL_ID
                        scoped = {"query":item.query, "active_entity":entity, "resolved_entity":entity,
                                  "selected_entity":entity, "entity_result":{"status":"UNIQUE","resolved_entity":entity},
                                  "business_intent":{"time_range":item.time_range.model_dump(mode="json")}}
                        args = {"required_entity_level":"area" if item.entity_level == "space" else "equipment"}
                        if item.operation == "health":
                            args["scope_type"] = "space" if item.entity_level == "space" else "device"
                            if item.time_range.mode == "range":
                                args.update(start_time=item.time_range.start_time, end_time=item.time_range.end_time)
                            if item.entity_level == "space" and item.time_range.mode == "range":
                                return {**row,"status":"NEEDS_INPUT","message":"区域健康度目前只支持实时查询。"}
                        else:
                            args.update(time_mode=item.time_range.mode if item.time_range.mode != "none" else "default", limit=20)
                            if item.time_range.mode == "range":
                                args.update(start_time=item.time_range.start_time, end_time=item.time_range.end_time)
                            elif item.time_range.mode in {"nearest", "latest_before"}:
                                args["target_time"] = item.time_range.target_time
                        arguments, missing = build_phm_data_arguments(tool_id=tool,state=scoped,call_arguments=args)
                        if missing:
                            return {**row,"status":"NEEDS_INPUT","message":"该对象尚缺少查询所需的信息。"}
                        data = await self.data.call_tool(tool.rsplit(".",1)[-1],arguments)
                        data = decode_phm_data_for_public(tool, data)
                    if data.get("success") is False:
                        return {**row,"status":"FAILED","message":"这项查询暂时没有取得可用结果。"}
                    return {**row, "status":"SUCCESS", "entity":entity, "result":data}
                except asyncio.CancelledError:
                    raise
                except Exception:
                    return {**row,"status":"FAILED","message":"这项查询暂时无法完成。"}
        results = await asyncio.gather(*(one(item) for item in queries))
        return ToolCallResult(tool_id=request.tool_id,status="SUCCESS",
                             content="已分别处理各个查询目标，未完成项已单独注明。",
                             structured_content={"items":results,"partial":any(x["status"]!="SUCCESS" or (x.get("result") or {}).get("status")=="PARTIAL" for x in results)})
