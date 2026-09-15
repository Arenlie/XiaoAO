"""Bind model-understood sensor filters to turn-scoped authoritative identities."""
from __future__ import annotations

from app.orchestration.entity_lifecycle import has_current_turn_entity_for_level
from app.orchestration.entity_dependency import structured_anchor
from app.tools.phm_sensor_mcp import SENSOR_EVIDENCE_TOOL_ID, SENSOR_TOOL_BY_OPERATION

SENSOR_ROUTING_RULES = """
传感器自检是独立的数据查询能力，工具部署在 PHM Asset MCP：
- 增加 sensor_query 对象，包含 operation（none/active/history/offline/monitoring/overview/points）、scope（global/equipment/point）、fault_type（具体故障名称或规则编码，没有类型条件时为null）、fault_status（无筛选为null）、time_field（start_time/end_time）、monitor_status（ONLINE/OFFLINE/SUSPENDED，无筛选null）、waveform_enabled（true/false/null）、feature_kind（bias/velocity/temperature，无筛选null）。这些是本轮语义，不能照抄上一轮筛选条件；明确指代上一轮条件时才承接，false是有效筛选。
- “监测了哪些/多少个测点”“有哪些在线或暂停测点”“哪些点配置了温度/偏置参数”“哪些点支持/不支持波形监听”使用points；该清单是传感器注册表，不是资产平台全部物理测点。只问资产设备有哪些测点仍走原资产查询。单点/设备是否在线使用monitoring。配置偏置参数与发生偏置电压异常是不同问题，后者必须active/history。
- points查询参数：在线清单monitor_status=ONLINE，暂停清单=SUSPENDED；泛指无新鲜数据的离线清单使用offline工具（包含OFFLINE和SUSPENDED）。温度监测配置feature_kind=temperature，偏置参数=bias，速度参数=velocity；波形监听waveform_enabled=true，不支持波形=false。监测点总数不等于当前在线点数，暂停点仍是已注册测点。
- points的limit默认50，用户明确要求列出全部可设1000，这是明细展示上限；只有上游实际完整性标记表明不完整时才说明来源截断；只问数量时说明注册总数、筛选命中数和本次展示数的区别。
- 单设备监测清单中的温度/偏置/波形是清单筛选条件，不要把这些配置条件填入资产measurement去筛设备；实体层仍保留用户真实设备/测点名称和明确范围。
- 用户问某设备或测点有没有偏置电压异常、偏置电压异常的信息、传感器温度异常、数据卡死、毛刺、歪度异常、波形削顶等已发布的传感器自检结果，operation=active，保留具体 fault_type。可识别“偏执电压”这类明显错别字。不能把偏置电压异常当成设备名称、空间或测点名称，也不能把它填进资产 measurement 来筛设备。
- “某设备是否在线”“这个测点离线了吗”“某设置是否在线”（语境中的设备错字）是 monitoring；“有哪些离线传感器”是 offline；历史/曾经/结束的故障查询是 history；同时要当前故障与在线离线概况是 overview。
- 单台设备的全部传感器仍以设备为输入，scope=equipment，anchor_entity_level=equipment，不要求用户先选一个测点；明确指定测点才是 scope=point。全局查询为global，anchor_entity_level=none，不继承旧设备。指定设备/测点绝不扩大为全局查询。
- 以上目标与成熟链路完全一致时，可选 sensor_information_query，variant_id 与 operation 相同。复合任务不完全匹配时用 none/none，让Planner组合传感器工具及其他实际需要的工具。
- 在线是传感器数据采集/监测状态，不等同于设备是否开机运行。纯粹询问“什么是偏置电压异常”而未查询现场数据时，sensor_query.operation=none，使用一般知识解释。
- 查询已经发布的某类传感器故障不等于要求机械故障专业诊断：diagnosis_requested=false，不能调用 Diagnosis MCP、健康度或综合报警来替代传感器查询；用户明确另有机械诊断目标时才组合其他证据。
- goal_frame.evidence_types 使用 sensor_active_faults、sensor_fault_history、sensor_monitoring、sensor_offline、sensor_points 表达所需的传感器证据。overview需要sensor_active_faults和sensor_offline。points需要sensor_points。独立的机械报警仍用alarm，二者不能相互代替。
- 需要真实身份时沿用统一实体解析和上下文切换。实体存在不证明传感器已监测；不在离线列表也不证明在线。监测范围、在线、离线、未监测和无法确认必须以工具返回为准。
- 历史查询使用本轮time_range，不把当前状态接口当历史在线查询。历史故障不代表当前仍故障。遇到截断/权限不足/接口失败不能回答“没有故障”。
""".strip()


def normalize_sensor_classification(value):
    """Enforce dependencies of an already model-classified sensor query, no NLP."""
    sensor = dict(value.get("sensor_query") or {})
    op = sensor.get("operation")
    if op not in SENSOR_TOOL_BY_OPERATION:
        return value
    goal = dict(value.get("goal_frame") or {})
    semantics = dict(value.get("asset_semantics") or {})
    scope = sensor.get("scope") or "equipment"
    if scope == "global":
        explicit_point = any((semantics.get(k) or {}).get("raw_text") for k in ("point", "point_no"))
        explicit_equipment = any((semantics.get(k) or {}).get("raw_text") for k in ("equipment", "equip_no"))
        reference = semantics.get("reference_target_level")
        if explicit_point or reference == "point":
            scope = "point"
        elif explicit_equipment or reference == "equipment":
            scope = "equipment"
        sensor["scope"] = scope
        value["sensor_query"] = sensor
    anchor = "none" if scope == "global" else scope
    goal["anchor_entity_level"] = anchor
    goal["target_entity_level"] = "point" if scope == "global" else scope
    goal["evidence_required"] = True
    goal["can_answer_from_context"] = False
    required = set(goal.get("requested_evidence_types") or [])
    if "diagnosis_result" not in required:
        goal["diagnosis_requested"] = False
        goal["diagnosis_request_confidence"] = 0.0
        goal["operations"] = [x for x in goal.get("operations") or [] if x != "diagnose"]
        if value.get("workflow_id") == "diagnosis_analysis":
            value["workflow_id"], value["variant_id"], value["recipe_recommended"] = "none", "none", False
    evidence = [e for e in goal.get("evidence_types") or [] if e not in {"alarm", "health", "diagnosis_result", "vibration", "temperature"} or e in required]
    needed = {"active": ["sensor_active_faults"], "history": ["sensor_fault_history"],
              "monitoring": ["sensor_monitoring"], "offline": ["sensor_offline"],
              "points": ["sensor_points"],
              "overview": ["sensor_active_faults", "sensor_offline"]}[op]
    goal["evidence_types"] = list(dict.fromkeys(evidence+needed))
    semantics["needs_asset_lookup"] = anchor != "none"
    value["goal_frame"], value["asset_semantics"] = goal, semantics
    return value


def sensor_required_level(state, arguments, tool_id=""):
    if tool_id == SENSOR_EVIDENCE_TOOL_ID:
        return "equipment"
    sensor = (state.get("business_intent") or {}).get("sensor_query") or {}
    declared = sensor.get("scope") if sensor.get("operation") not in {None, "none"} else None
    scope = declared or arguments.get("scope")
    if scope in {"equipment", "point"}:
        return scope
    anchor = structured_anchor(state)
    # A scoped goal can never silently broaden to every device because a planner
    # forgot its identity requirement. Global questions may ignore a previous anchor.
    if declared == "global":
        return "none"
    if anchor in {"equipment", "point", "area"}:
        return anchor
    if scope == "global":
        return "none"
    requested = arguments.get("required_entity_level")
    return requested if requested in {"equipment", "point", "area"} else "equipment"


def build_sensor_arguments(tool_id, state, call_arguments):
    from app.orchestration.sensor_identity import available
    target = state.get("sensor_target") or {}
    level = sensor_required_level(state, call_arguments, tool_id)
    if level == "area":
        return {}, ["传感器查询需要具体设备或测点；区域范围需先取得真实设备集合"]
    if level != "none" and not available(state, level) and not has_current_turn_entity_for_level(state, level):
        return {}, ["本轮尚未确认查询对象，需先完成实体检索"]
    entity = state.get("selected_entity") or state.get("resolved_entity") or state.get("active_entity") or {}
    entity = {**(entity.get("metadata") or {}), **entity}
    if available(state, level) and level != "none":
        entity = {"equip_no": target["equip_num"], "point_no": target.get("point_num")}
    result = {}
    if level != "none":
        equip = entity.get("equip_no") or entity.get("device_code") or entity.get("equipment_no") or entity.get("equipNo")
        if not equip:
            return {}, ["实体结果缺少真实设备编码"]
        result["equip_num"] = str(equip)
        if level == "point":
            point = entity.get("point_no") or entity.get("pointNo")
            if not point:
                return {}, ["实体结果缺少真实测点编码"]
            result["point_num"] = str(point)
    if tool_id == SENSOR_EVIDENCE_TOOL_ID:
        fid = str(call_arguments.get("fault_id") or target.get("fault_id") or "").strip()
        known = set()
        if target.get("fault_id"):
            known.add(str(target["fault_id"]))
        for observation in state.get("observations") or []:
            if observation.get("tool_id") not in SENSOR_TOOL_BY_OPERATION.values():
                continue
            data = (observation.get("tool_result") or {}).get("structured_content") or {}
            for row in data.get("records") or []:
                if str(row.get("equip_num") or "").casefold() == result["equip_num"].casefold():
                    known.add(str(row.get("fault_id") or ""))
        if not fid or (fid not in known and fid not in str(state.get("query") or "")):
            return {}, ["请先查询当前设备的真实故障列表以取得故障编号"]
        result["fault_id"] = fid
        if target.get("fault_id") == fid and target.get("point_num"):
            result["point_num"] = target["point_num"]
        if call_arguments.get("refresh") or ((state.get("business_intent") or {}).get("asset_semantics") or {}).get("refresh_requested"):
            result["refresh"] = True
        return result, []
    for key in ("limit", "include_analysis", "refresh"):
        if key in call_arguments:
            result[key] = call_arguments[key]
    intent = state.get("business_intent") or {}
    if intent.get("classification_degraded"):
        return {}, ["本轮筛选条件尚未可靠解析，未扩大范围执行传感器查询；已定位到的原故障仍可读取详情。"]
    sensor = intent.get("sensor_query") or {}
    if tool_id in {SENSOR_TOOL_BY_OPERATION[x] for x in ("active", "monitoring", "offline", "overview", "points")} and (intent.get("time_range") or {}).get("mode") in {"range", "nearest", "latest_before"}:
        return {}, ["当前故障和在线/离线接口只提供当前快照，不能还原指定历史时刻的状态；已结束故障需使用历史故障查询"]
    if tool_id == SENSOR_TOOL_BY_OPERATION["points"]:
        result["limit"] = sensor.get("limit", 50) if sensor.get("operation") == "points" else call_arguments.get("limit", 50)
        for key in ("monitor_status", "waveform_enabled", "feature_kind"):
            # Current classified null explicitly clears a previous filter. Planner
            # arguments supply filters only for composite/unclassified operations.
            value = sensor.get(key) if sensor.get("operation") == "points" else call_arguments.get(key)
            if value is not None:
                result[key] = value
        result.pop("include_analysis", None)
    if tool_id in {SENSOR_TOOL_BY_OPERATION[x] for x in ("active", "history", "overview")}:
        fault_type = sensor.get("fault_type") or call_arguments.get("fault_type")
        if fault_type:
            result["fault_type"] = str(fault_type)
        if sensor.get("fault_status"):
            result["fault_status"] = sensor["fault_status"]
    if tool_id == SENSOR_TOOL_BY_OPERATION["history"]:
        time_range = intent.get("time_range") or {}
        if time_range.get("mode") in {"nearest", "latest_before"}:
            return {}, ["历史故障接口只能按发生或结束时间段筛选已取回的记录，不能保证还原某一时刻或找到该时刻最近的完整故障记录"]
        if time_range.get("mode") == "range":
            field = sensor.get("time_field", call_arguments.get("time_field", "start_time"))
            if field not in {"start_time", "end_time"}:
                return {}, ["历史故障时间筛选字段无效"]
            if not time_range.get("start_time") or not time_range.get("end_time"):
                return {}, ["历史故障时间范围尚未完整解析"]
            result[field+"_from"] = time_range["start_time"]
            result[field+"_to"] = time_range["end_time"]
    if (intent.get("asset_semantics") or {}).get("refresh_requested"):
        result["refresh"] = True
    return result, []


def format_sensor_result(payload):
    lines = [str(payload.get("message") or "传感器查询已完成。")]
    if payload.get("query_type") == "FAULT_EVIDENCE":
        row = payload.get("record") or {}
        lines.append(f"{row.get('point_name') or row.get('point_num') or '该测点'}：{row.get('fault_type_name') or '传感器故障'}，处理状态：{row.get('fault_status_name') or '未提供'}。")
        if row.get("analysis_result"):
            lines.append("传感器服务已有的 AI 复核：\n" + str(row["analysis_result"]))
        else:
            lines.append("该记录未提供 AI 复核内容。")
        if row.get("analysis_complete") is False:
            lines.append("当前分析内容未完整返回，以上仅为已取得部分。")
        return "\n\n".join(lines)
    if payload.get("query_type") == "MONITORED_SENSOR_POINTS":
        for row in (payload.get("records") or [])[:12]:
            name = row.get("point_name") or row.get("point_num") or "测点"
            features = "、".join(str(p.get("param_name") or {"bias":"偏置电压", "velocity":"速度有效值", "temperature":"温度"}.get(p.get("feature_kind"), "特征参数")) for p in row.get("feature_params") or [])
            wave = "已监听波形" if row.get("waveform_enabled") is True else "未监听波形" if row.get("waveform_enabled") is False else "波形监听配置未提供"
            lines.append(f"- {name}：{row.get('monitoring_status_name') or '状态未确认'}；{wave}；自检参数：{features or '未提供配置或未配置'}。")
        lines.extend(str(w) for w in payload.get("warnings") or [])
        return "\n".join(lines)
    coverage = payload.get("coverage") or {}
    if coverage.get("scope") == "global":
        lines.append(f"服务已配置逻辑测点：{coverage.get('configured_sensor_count')}；当前在线监测：{coverage.get('online_sensor_count')}；离线：{coverage.get('offline_sensor_count')}。")
    elif coverage:
        lines.append(f"已核对测点：{coverage.get('checked_logical_points')}；在线：{coverage.get('online_point_count')}；离线：{coverage.get('offline_point_count')}；未纳入监测：{coverage.get('unmonitored_point_count')}；未确认：{coverage.get('unknown_point_count')}。")
    for row in (payload.get("records") or [])[:12]:
        name = row.get("point_name") or row.get("point_num") or "测点"
        if row.get("fault_type_name"):
            lines.append(f"- {name}：{row['fault_type_name']}，{row.get('fault_status_name') or '状态未注明'}，开始时间：{row.get('start_time') or '未提供'}。")
        else:
            lines.append(f"- {name}：{row.get('monitoring_status_name') or '暂时无法确认'}。")
    lines.extend(str(w) for w in payload.get("warnings") or [])
    return "\n".join(lines)
