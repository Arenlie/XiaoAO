"""Sensor and asset identities are verified independently for one user turn."""
from __future__ import annotations

import hashlib
import json

from app.performance import detail_span
from app.services.sensor_references import code_tokens, matches_tokens, referenced_fault


def target_revision(target):
    return hashlib.sha256(json.dumps({k: target.get(k) for k in ("equip_num", "point_num", "fault_id")},
        sort_keys=True).encode()).hexdigest()[:24]


def available(state, level="point"):
    target = state.get("sensor_target") or {}
    return bool(target.get("verified") and target.get("equip_num") and
                matches_tokens(target, code_tokens(state.get("query"))) and
                (level != "point" or target.get("point_num")))


async def prepare_identity(nodes, state, classification, *, refresh=False):
    state = {**state, "business_intent": classification}
    sensor = dict(classification.get("sensor_query") or {})
    ref, ambiguity = referenced_fault(state)
    tokens = code_tokens(state.get("query"))
    if not ref and not ambiguity and (sensor.get("operation") in {None, "none"} or not tokens):
        return None  # Names and ordinary contextual assets use the established layer.
    if not nodes.entity_resolution_layer:
        return None
    from app.tools.phm_sensor_mcp import SENSOR_TOOL_BY_OPERATION
    if not any(t.enabled and t.tool_id == SENSOR_TOOL_BY_OPERATION["points"] for t in nodes.tool_registry.list_descriptors()):
        return None
    if ref and not tokens:
        tokens = [ref["equip_num"], ref["point_num"]]
    resolution = {}
    error = ambiguity
    if tokens and not ambiguity:
        async with detail_span(state, code="sensor.identity.exact", name="核对传感器与设备编码",
                description="分别核验传感器登记记录与设备档案，不用相似名称替换编码", category="mcp") as span:
            try:
                payload = await nodes.entity_resolution_layer.client.call_tool("query_monitored_sensor_points",
                    {"identity_tokens": tokens, "refresh": refresh,
                     "request_id": str(state.get("task_id") or "")})
                resolution = payload.get("identity_resolution") or {}
                if not resolution:
                    error = "传感器编码核验未返回有效对应关系，请确认资产服务已同步升级。"
            except Exception:
                error = "传感器或设备编码核验暂未完成，不能据此判断无故障。"
            if span is not None:
                span["metrics"].update(input_tokens=tokens, match_count=resolution.get("match_count", 0),
                    registry=resolution.get("registry") or {}, reference_message_id=(ref or {}).get("source_message_id"))
    targets = [t for t in resolution.get("targets") or [] if matches_tokens(t, tokens)]
    target = {}
    if ref:
        # A persisted successful fault record independently authorizes reading its
        # detail. A missing asset point must not discard this verified reference.
        target = {**ref, "verified": True, "identity_source": "message_fault_reference"}
        exact = [t for t in targets if t.get("equip_num") == ref["equip_num"] and t.get("point_num") == ref["point_num"]]
        if len(exact) == 1:
            target.update(aliases=exact[0].get("aliases") or [])
        identity_only = sensor.get("operation") in {"monitoring", "offline", "points"} or (
            sensor.get("fault_type") and sensor["fault_type"] not in {ref.get("fault_type_name"), ref.get("rule_code")})
        if identity_only:
            target = {k: v for k, v in target.items() if k in {"equip_num", "point_num", "param_num", "point_name",
                "aliases", "verified", "identity_source", "source_message_id"}}
    elif len(targets) == 1 and not resolution.get("output_limited"):
        target = {**targets[0], "verified": True}
    elif len(targets) > 1:
        error = "输入编码对应多个传感器逻辑测点，请补充所属设备或逻辑测点编码。"
    assets = resolution.get("asset_entities") or []
    if not target and not ambiguity:
        exact = [a for a in assets if matches_tokens({"equip_num": a.get("equip_no"), "point_num": a.get("point_no")}, tokens)]
        points = [a for a in exact if a.get("point_no")]
        exact = points or [a for a in exact if a.get("equip_no")]
        if len(exact) == 1:
            target = {"equip_num": exact[0]["equip_no"], "point_num": exact[0].get("point_no"),
                      "verified": True, "identity_source": "asset_catalog"}
    equipment = [a for a in assets if a.get("entity_type") == "equipment" and target.get("equip_num")
                 and str(a.get("equip_no") or "").casefold() == target["equip_num"].casefold()]
    actual_asset = equipment[0] if len(equipment) == 1 else {}
    if resolution.get("asset_output_limited"):
        actual_asset = {}
    if target:
        point_assets = [a for a in assets if a.get("entity_type") == "point"
                       and str(a.get("equip_no") or "").casefold() == target["equip_num"].casefold()
                       and a.get("point_no") in {target.get("point_num"), *(target.get("aliases") or [])}]
        target["asset_points"] = point_assets
    if not target and not error:
        error = "未找到与输入编码一致的传感器或设备记录，请核对原始编码；本次没有使用其他设备替代。"
    if target:
        target["revision"] = target_revision(target)
        target["input_tokens"] = code_tokens(state.get("query"))
        target["asset_identity_available"] = bool(actual_asset)
        sensor["scope"] = "point" if target.get("point_num") else "equipment"
        if sensor.get("operation") in {None, "none"}:
            sensor["operation"] = "active"
        if target.get("fault_id") and not sensor.get("fault_type"):
            sensor["fault_type"] = ref.get("fault_type_name")
    from app.tools.phm_sensor_context import normalize_sensor_classification
    classification = normalize_sensor_classification({**classification, "sensor_query": sensor,
        "workflow_id": "none", "variant_id": "none", "recipe_recommended": False})
    status = "UNIQUE" if actual_asset else "NO_LOOKUP"
    result = {"status": status, "need_lookup": bool(actual_asset), "need_disambiguation": False,
        "resolved_entity": actual_asset or None, "matches": [actual_asset] if actual_asset else [],
        "match_count": int(bool(actual_asset)), "resolution_source": "sensor_identity_verification",
        "message": error or "已核验传感器查询对象，设备档案单独对应。"}
    return {"sensor_target": target, "sensor_identity_attempted": True, "sensor_identity_error": error,
        "sensor_identity_resolution": resolution, "resolved_entity": actual_asset,
        "selected_entity": {}, "active_entity": actual_asset, "invalidate_active_entity": True,
        "entity_result": result, "entity_resolution": {"action": "replace", "source": "sensor_identity_verification"},
        "entity_dependency": {"required": False, "level": "none", "source": "sensor_identity_verification"},
        "business_intent": classification, "business_workflow": {}, "clarification_context": {},
        "observations": [{"call_id": "sensor-identity-" + str(state.get("graph_attempt") or 0),
            "tool_id": "mcp.phm_asset.query_monitored_sensor_points", "status": "COMPLETED",
            "identity_only": True, "can_support_final_answer": bool(target),
            "answer_markdown": error or ("已确认传感器记录及设备编码。" if actual_asset else "已确认传感器记录，设备档案暂未对应成功。"),
            "evidence": [{"source_type": "sensor_identity", "content": resolution}]}]}


def validate_result(state, arguments, payload):
    """Request/response identity fence; empty wrong-target results are rejected too."""
    target = state.get("sensor_target") or {}
    if target and not matches_tokens(target, [v for k, v in arguments.items() if k in {"equip_num", "point_num"} and v]):
        return False
    for key in ("equip_num", "point_num", "fault_id"):
        expected = arguments.get(key)
        reported = (payload.get("filters") or {}).get(key)
        if expected and reported and str(expected).casefold() != str(reported).casefold():
            return False
    rows = [payload["record"]] if isinstance(payload.get("record"), dict) else payload.get("records") or []
    allowed_points = {arguments.get("point_num"), target.get("point_num")}
    if arguments.get("point_num"):
        mapped = []
        live_aliases = set()
        for row in (payload.get("coverage") or {}).get("records") or []:
            aliases = {str(row[k]) for k in ("point_num", "raw_point_no", "vibration_point_num", "temperature_point_num",
                "bias_param_num", "velocity_param_num", "temperature_param_num") if row.get(k)}
            for feature in row.get("feature_params") or []:
                aliases.update(str(feature[k]) for k in ("point_no", "param_code") if feature.get(k))
            if str(row.get("equip_num") or "").casefold() == str(arguments.get("equip_num") or "").casefold():
                live_aliases.update(aliases)
            if (arguments["point_num"] in aliases and
                    str(row.get("equip_num") or "").casefold() == str(arguments.get("equip_num") or "").casefold()):
                mapped.append(row.get("point_num"))
        if len(set(mapped)) == 1:
            allowed_points.update(mapped)
        point_inputs = [x for x in target.get("input_tokens") or []
                        if x.casefold() != str(target.get("equip_num") or "").casefold()]
        if live_aliases and point_inputs and not set(point_inputs).issubset(live_aliases):
            return False  # A changed alias mapping requires one fresh exact lookup.
    for row in rows:
        if arguments.get("equip_num") and str(row.get("equip_num") or "").casefold() != str(arguments["equip_num"]).casefold():
            return False
        if arguments.get("point_num") and row.get("point_num") not in allowed_points:
            return False
        if arguments.get("fault_id") and row.get("fault_id") != arguments["fault_id"]:
            return False
        if arguments.get("fault_id") and target.get("fault_id") == arguments["fault_id"]:
            if target.get("param_num") and row.get("param_num") and target["param_num"] != row["param_num"]:
                return False
            if target.get("start_time") and row.get("start_time"):
                from datetime import datetime
                try:
                    before = datetime.fromisoformat(str(target["start_time"]).replace("Z", "+00:00"))
                    after = datetime.fromisoformat(str(row["start_time"]).replace("Z", "+00:00"))
                    if before != after:
                        return False
                except ValueError:
                    return False
    return True


def fault_from_current_result(state, tool_id, payload):
    """A first coded analysis can discover its fault id without old message metadata."""
    import re
    from app.tools.phm_sensor_mcp import SENSOR_TOOL_BY_OPERATION
    target = state.get("sensor_target") or {}
    if (not target.get("verified") or target.get("fault_id")
            or tool_id not in {SENSOR_TOOL_BY_OPERATION[x] for x in ("active", "history", "overview")}
            or payload.get("success") is False):
        return None
    query = str(state.get("query") or "")
    fids = re.findall(r"FLT-[A-Za-z0-9_-]+", query, re.I)
    if not fids and not re.search(r"分析|原因|结论|复核|详细依据|[Aa][Ii]", query):
        return None
    candidates = [row for row in payload.get("records") or [] if row.get("fault_id")
                  and row.get("equip_num") and row.get("point_num")
                  and (not fids or row["fault_id"] in fids)]
    if len(candidates) != 1 or (not target.get("point_num") and not fids):
        return None
    row = candidates[0]
    if not matches_tokens(row, [target[k] for k in ("equip_num", "point_num") if target.get(k)]):
        return None
    # Keep this scope revision: discovering one fault does not invalidate the
    # verified current-status observation for the same equipment and point.
    fields = {k: row[k] for k in ("fault_id", "param_num", "fault_type_name", "rule_code",
        "fault_status", "fault_status_name", "start_time", "end_time") if row.get(k) is not None}
    return {**target, **fields, "point_num": row["point_num"],
            "analysis_result": str(row.get("analysis_result") or "")[:1200],
            "analysis_complete": row.get("analysis_complete") is True and len(str(row.get("analysis_result") or "")) <= 1200}


def sensor_verdict(state, descriptors, settings):
    """Compose independent reads once a sensor identity/reference is verified."""
    if not state.get("sensor_identity_attempted"):
        return None
    from uuid import uuid4
    from app.orchestration.supervisor.contracts import SupervisorVerdict, SupervisorVerdictType, AgentCall
    from app.tools.phm_sensor_mcp import SENSOR_EVIDENCE_TOOL_ID, SENSOR_TOOL_BY_OPERATION
    from app.tools.phm_asset_mcp import PHM_ASSET_QUERY_EQUIPMENT_INFO_TOOL_ID
    from app.tools.dify_knowledge import KNOWLEDGE_TOOL_ID
    from app.orchestration.knowledge_enrichment import needs_enrichment, enrichment_query
    target = state.get("sensor_target") or {}
    mismatches = [o for o in state.get("observations") or [] if o.get("error_code") == "SENSOR_IDENTITY_MISMATCH"]
    if mismatches and int(state.get("sensor_identity_corrections") or 0) < 1:
        from app.agents.catalog import FUZZY_ENTITY_AGENT_ID
        return SupervisorVerdict(verdict=SupervisorVerdictType.NEED_MORE_EVIDENCE,
            next_call=AgentCall(call_id=str(uuid4()), agent_id=FUZZY_ENTITY_AGENT_ID,
                objective="重新核对原始查询编码：" + " ".join(code_tokens(state.get("query")) or
                    [str(target[k]) for k in ("equip_num", "point_num") if target.get(k)]),
                arguments={"required_entity_level": "point" if target.get("point_num") else "equipment"}))
    if not target.get("verified"):
        return SupervisorVerdict(verdict=SupervisorVerdictType.ANSWERABLE, answerable=True,
            final_answer_draft=state.get("sensor_identity_error") or "尚未取得与该编码一致的记录，无法判断故障状态。",
            reason_summary="编码未唯一对应，本轮说明缺口并结束，允许继续输入。")
    enabled = {t.tool_id for t in descriptors if t.enabled}
    attempted = {o.get("tool_id") for o in state.get("observations") or []
                 if not o.get("identity_only") and o.get("sensor_target_revision", target.get("revision")) == target.get("revision")}
    sensor = (state.get("business_intent") or {}).get("sensor_query") or {}
    calls = []
    def add(tool_id, objective, args):
        if tool_id and tool_id in enabled and tool_id not in attempted:
            calls.append(AgentCall(call_id=str(uuid4()), call_type="tool", tool_id=tool_id,
                                  objective=objective, arguments=args))
    if target.get("fault_id"):
        add(SENSOR_EVIDENCE_TOOL_ID, "读取这条故障已有的人工处理状态及 AI 复核原文",
            {"fault_id": target["fault_id"]})
    if not (state.get("business_intent") or {}).get("classification_degraded"):
        add(SENSOR_TOOL_BY_OPERATION.get(sensor.get("operation")), "核验当前询问范围的传感器状态与故障记录",
            {"scope": "point" if target.get("point_num") else "equipment", "fault_type": sensor.get("fault_type")})
    if target.get("asset_identity_available"):
        add(PHM_ASSET_QUERY_EQUIPMENT_INFO_TOOL_ID, "补充已核验设备的真实档案，辅助解释传感器故障", {})
        from app.tools.phm_data_mcp import PHM_QUERY_HEALTH_SCORE_TOOL_ID, PHM_QUERY_ALARM_RECORDS_TOOL_ID
        intent = state.get("business_intent") or {}
        requested = set((intent.get("goal_frame") or {}).get("requested_evidence_types") or [])
        period = intent.get("time_range") or {}
        if "health" in requested:
            args = {"scope_type": "device", "required_entity_level": "equipment"}
            if period.get("mode") == "range":
                args.update(start_time=period.get("start_time"), end_time=period.get("end_time"))
            add(PHM_QUERY_HEALTH_SCORE_TOOL_ID, "补充问题明确要求的设备健康度", args)
        if "alarm" in requested:
            args = {"required_entity_level": "equipment", "time_mode": period.get("mode") or "default", "limit": 20}
            if args["time_mode"] == "none":
                args["time_mode"] = "default"
            if period.get("mode") == "range":
                args.update(start_time=period.get("start_time"), end_time=period.get("end_time"))
            elif period.get("mode") in {"nearest", "latest_before"}:
                args["target_time"] = period.get("target_time")
            add(PHM_QUERY_ALARM_RECORDS_TOOL_ID, "补充问题明确要求的设备报警", args)
    if needs_enrichment(state, settings):
        add(KNOWLEDGE_TOOL_ID, "检索与这条故障有关的专业原理、维护依据及历史案例",
            {"query": enrichment_query(state)})
    if calls:
        return SupervisorVerdict(verdict=SupervisorVerdictType.NEED_MORE_EVIDENCE,
            next_calls=calls, next_call=calls[0], reason_summary="并行读取传感器证据、已核验的设备档案和相关知识。")
    return SupervisorVerdict(verdict=SupervisorVerdictType.ANSWERABLE, answerable=True,
        reason_summary="传感器相关查询已经完成或明确失败，综合已有证据并说明缺口。")


def synthesis_observations(state):
    """Keep old/wrong identity payloads out of final evidence, including zero rows."""
    import copy
    from app.tools.phm_sensor_mcp import PHM_SENSOR_TOOL_IDS, SENSOR_EVIDENCE_TOOL_ID
    from app.services.sensor_references import current_observations
    output = []
    for observation in current_observations(state):
        if observation.get("error_code") == "SENSOR_IDENTITY_MISMATCH":
            output.append({"status": "FAILED", "answer_markdown": observation.get("answer_markdown"),
                           "can_support_final_answer": False})
        elif observation.get("tool_id") in PHM_SENSOR_TOOL_IDS and not observation.get("identity_only"):
            item = {k: observation.get(k) for k in ("tool_id", "status", "can_support_final_answer", "error_code", "error_message")}
            data = copy.deepcopy((observation.get("tool_result") or {}).get("structured_content") or {})
            records = [data["record"]] if isinstance(data.get("record"), dict) else data.get("records") or []
            if len(records) > 100:
                data["records"] = records[:100]
                data["model_records_limited"] = True
                records = records[:100]
            budget = 16000 if observation.get("tool_id") == SENSOR_EVIDENCE_TOOL_ID else 6000
            for row in records:
                text = str(row.get("analysis_result") or "")
                allowance = min(budget, 16000 if "record" in data else 1200)
                if len(text) > allowance:
                    row["analysis_result"] = text[:allowance]
                    row["analysis_complete"] = False
                budget -= min(len(text), allowance)
            item["tool_result"] = {"structured_content": data}
            output.append(item)
        else:
            output.append(observation)
    return sorted(output, key=lambda o: 0 if o.get("tool_id") == SENSOR_EVIDENCE_TOOL_ID else 1)
