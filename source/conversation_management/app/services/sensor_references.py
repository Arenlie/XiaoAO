"""Bounded fault references on the owned parent-message path, never live status."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone

from app.tools.phm_sensor_mcp import PHM_SENSOR_TOOL_IDS

TTL_SECONDS = 86400
_CODE = re.compile(r"(?<![A-Za-z0-9_.-])[A-Za-z][A-Za-z0-9_-]{3,255}(?![A-Za-z0-9_.-])")


def code_tokens(text):
    text = unicodedata.normalize("NFKC", str(text or ""))
    return list(dict.fromkeys(m[0] for m in _CODE.finditer(text)
        if any(c.isdigit() for c in m[0]) and not m[0].upper().startswith("FLT-")
        and not re.search(r"(?:型号|标准号?|版本|协议)\s*[:：为是]?\s*$", text[max(0, m.start()-12):m.start()])))[:32]


def record_codes(row):
    return {str(row.get(k)) for k in ("equip_num", "point_num", "param_num") if row.get(k)} | set(row.get("aliases") or [])


def matches_tokens(row, tokens):
    codes = record_codes(row)
    return all(t in codes or t.casefold() == str(row.get("equip_num") or "").casefold() for t in tokens)


def payload_records(observation):
    data = (observation.get("tool_result") or {}).get("structured_content") or {}
    return ([data["record"]] if isinstance(data.get("record"), dict) else data.get("records") or [])


def current_observations(state):
    """Do not reuse evidence obtained before this turn's identity correction."""
    revision = (state.get("sensor_target") or {}).get("revision")
    return [obs for obs in state.get("observations") or []
            if not revision or not obs.get("sensor_target_revision")
            or obs["sensor_target_revision"] == revision]


def persist_references(state, answer):
    rows = []
    for obs in current_observations(state):
        if not obs.get("can_support_final_answer") or obs.get("error_code") == "SENSOR_IDENTITY_MISMATCH":
            continue
        candidates = payload_records(obs) if obs.get("tool_id") in PHM_SENSOR_TOOL_IDS else []
        if obs.get("tool_id") == "workflow.phm.independent_queries":
            data = (obs.get("tool_result") or {}).get("structured_content") or {}
            for item in data.get("items") or []:
                if item.get("status") == "SUCCESS" and str(item.get("operation") or "").startswith("sensor_"):
                    result = item.get("result") or {}
                    candidates += [result["record"]] if isinstance(result.get("record"), dict) else result.get("records") or []
        for row in candidates:
            if row.get("fault_id") and row.get("equip_num") and row.get("point_num"):
                rows.append(row)
    if not rows and (state.get("sensor_target") or {}).get("fault_id"):
        rows = [state["sensor_target"]]
    saved, seen = [], set()
    for row in rows:
        key = tuple(str(row.get(k) or "") for k in ("fault_id", "equip_num", "point_num", "start_time"))
        if key in seen:
            continue
        seen.add(key)
        clean = {k: row.get(k) for k in ("fault_id", "equip_num", "point_num", "param_num", "point_name", "rule_code",
            "fault_type_name", "fault_status", "fault_status_name", "start_time", "end_time") if row.get(k) is not None}
        clean["analysis_result"] = str(row.get("analysis_result") or "")[:1200]
        clean["analysis_complete"] = row.get("analysis_complete") is True and len(str(row.get("analysis_result") or "")) <= 1200
        # Only assign an ordinal that can be verified in the actual displayed row.
        # Model/tool order is never assumed to equal display order.
        indexes = []
        for line in str(answer).splitlines():
            if str(row["fault_id"]) in line or (str(row["point_num"]) in line and
                    (str(row.get("fault_type_name") or "__unknown__") in line)):
                match = re.match(r"\s*(?:\|\s*)?(\d+)\s*(?:[.、)）]|\|)", line)
                if match:
                    indexes.append(int(match[1]))
        clean["display_index"] = indexes[0] if len(set(indexes)) == 1 else None
        clean["source_message_id"] = str(state.get("assistant_message_id") or "")
        clean["captured_at"] = datetime.now(timezone.utc).isoformat()
        saved.append(clean)
        if len(saved) >= 100:
            break
    return {"sensor_fault_references": saved} if saved else {}


def load_references(messages, *, now=None):
    now = now or datetime.now(timezone.utc)
    batches = []
    for message in reversed(messages):
        rows = (getattr(message, "metadata_json", None) or {}).get("sensor_fault_references") or []
        valid = []
        for row in rows[:100]:
            if not isinstance(row, dict):
                continue
            try:
                age = (now - datetime.fromisoformat(row["captured_at"])).total_seconds()
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= age <= TTL_SECONDS and row.get("fault_id") and row.get("equip_num") and row.get("point_num"):
                valid.append({**row, "source_message_id": str(message.id)})
        if valid:
            batches.append(valid)
        if len(batches) >= 6:
            break
    return batches


def referenced_fault(state):
    query = str(state.get("query") or "")
    batches = (state.get("memory_context") or {}).get("sensor_fault_references") or []
    tokens = code_tokens(query)
    fids = re.findall(r"FLT-[A-Za-z0-9_-]+", query, re.I)
    if tokens or fids:
        for rows in batches:
            matched = [r for r in rows if matches_tokens(r, tokens) and (not fids or r.get("fault_id") in fids)]
            kind = ((state.get("business_intent") or {}).get("sensor_query") or {}).get("fault_type")
            if kind:
                narrowed = [r for r in matched if kind in {r.get("fault_type_name"), r.get("rule_code")}]
                if narrowed:
                    matched = narrowed
                elif matched:
                    return None, None  # A new fault type is a new query, not the old record.
            if len(matched) == 1:
                return matched[0], None
            if len(matched) > 1:
                return (None, "同一测点有多条故障记录，请补充故障类型或故障开始时间。") if fids or re.search(r"分析|结论|原因|这条", query) else (None, None)
        return None, None
    ordinal = re.search(r"第\s*([0-9]+|[一二三四五六七八九十])\s*[条项个]", query)
    if ordinal and batches:
        raw = ordinal[1]
        index = int(raw) if raw.isdigit() else "一二三四五六七八九十".index(raw) + 1
        matched = [r for r in batches[0] if r.get("display_index") == index]
        if len(matched) == 1:
            return matched[0], None
        return None, "未能从上一条回答中唯一对应这条故障，请粘贴该条记录或测点编码。"
    semantics = (state.get("business_intent") or {}).get("asset_semantics") or {}
    explicit = any((semantics.get(k) or {}).get("raw_text") for k in ("equipment", "point", "equip_no", "point_no", "area"))
    followup = (semantics.get("reference_target_level") == "point" or re.search(
        r"(这[个条次].*(故障|测点|传感器)|直接.*结论|继续.*分析|分析.*原因|它.*(故障|原因|异常|在线|离线)|AI.*分析|[Aa][Ii].*复核)", query))
    if batches and followup and not explicit:
        if len(batches[0]) == 1:
            return batches[0][0], None
        return None, "上一条回答包含多条传感器故障，请说明要分析第几条，或粘贴测点编码。"
    return None, None
