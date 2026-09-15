"""Authoritative sensor registry; exact observed aliases, explicit completeness."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.providers.sensor_agent import SensorError

PARAM_FIELDS = {"param_code", "param_name", "feature_kind", "point_no", "suffix", "params_unit",
                "lower_limit", "upper_limit", "alarm_value", "slight_upper", "severity_upper"}
POINT_FIELDS = {"equip_name", "raw_point_no", "point_name", "point_types", "sort_no", "waveform_enabled",
                "vibration_point_num", "temperature_point_num", "bias_param_num", "velocity_param_num",
                "temperature_param_num", "monitor_status", "monitor_status_name", "monitor_status_source",
                "suspended_until_next_sync"}
SUMMARY_COUNTS = {"logical_point_count", "feature_param_count", "waveform_point_count",
                  "suspended_point_count", "explicit_offline_point_count"}


def point_aliases(row):
    result = {str(row[k]) for k in ("point_num", "point_no", "raw_point_no", "vibration_point_num", "temperature_point_num",
                                   "bias_param_num", "velocity_param_num", "temperature_param_num") if row.get(k)}
    for feature in row.get("feature_params") or []:
        for key in ("point_no", "param_code"):
            if feature.get(key):
                result.add(str(feature[key]))
    return result


def identity(row):
    return str(row.get("equip_num") or row.get("equip_no") or "").casefold(), str(row.get("point_num") or row.get("point_no") or "")


def monitoring_point(row, source, *, evidence="sensor_registry"):
    result = {k: row[k] for k in POINT_FIELDS if k in row}
    result.update(equip_num=row.get("equip_num") or row.get("equip_no"), point_num=row.get("point_num") or row.get("point_no"))
    params = row.get("feature_params")
    result["feature_params"] = [{k: v for k, v in p.items() if k in PARAM_FIELDS} for p in params] if isinstance(params, list) else None
    raw_status = row.get("monitor_status")
    status = "ONLINE" if raw_status == "ONLINE" else "OFFLINE" if raw_status in {"OFFLINE", "SUSPENDED"} else "UNKNOWN"
    name = {"ONLINE": "在线监测", "OFFLINE": "离线，当前没有新鲜数据", "SUSPENDED": "暂停诊断，当前没有新鲜数据"}.get(raw_status, "暂时无法确认")
    result.update(monitored=True, monitoring_status=status, monitoring_status_name=name,
        upstream_monitor_status=raw_status, upstream_monitor_status_source=row.get("monitor_status_source"),
        status_source="DATA_FRESHNESS" if row.get("monitor_status_source") == "history_cache_freshness" else row.get("monitor_status_source"),
        evidence_source=evidence, source=source)
    # An absent key is unknown; an explicit empty field means not configured.
    result["bias_input_configured"] = bool(row["bias_param_num"]) if isinstance(row.get("bias_param_num"), str) else (
        any(p.get("feature_kind") == "bias" and p.get("param_code") for p in params) if isinstance(params, list) else None)
    return result


@dataclass
class SensorRegistry:
    summary: dict[str, Any]
    records: list[dict[str, Any]]
    source: dict[str, Any]
    complete: bool
    by_equipment: dict = field(init=False, repr=False)
    by_alias: dict = field(init=False, repr=False)

    def __post_init__(self):
        self.by_equipment, self.by_alias = {}, {}
        for row in self.records:
            self.by_equipment.setdefault(identity(row)[0], []).append(row)
            for alias in point_aliases(row):
                self.by_alias.setdefault(alias, []).append(row)

    @classmethod
    def parse(cls, response):
        def bad():
            raise SensorError("SENSOR_AGENT_BAD_RESPONSE", "监测测点清单结构或总数异常，无法确认是否完整。", path=response["source"]["endpoint"])
        data = response["body"].get("data")
        if not isinstance(data, dict) or not isinstance(data.get("summary"), dict) or not isinstance(data.get("records"), list):
            bad()
        summary, rows = data["summary"], data["records"]
        if "logical_point_count" not in summary:
            bad()
        for key in SUMMARY_COUNTS & summary.keys():
            if type(summary[key]) is not int or summary[key] < 0:
                bad()
        total = summary["logical_point_count"]
        if len(rows) > total:
            bad()
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k].strip() for k in ("equip_no", "point_no")):
                bad()
            if identity(row) in seen:
                bad()
            seen.add(identity(row))
            params = row.get("feature_params")
            if params is not None and (not isinstance(params, list) or any(not isinstance(p, dict) for p in params)):
                bad()
            if row.get("waveform_enabled") is not None and type(row["waveform_enabled"]) is not bool:
                bad()
            if row.get("monitor_status") is not None and not isinstance(row["monitor_status"], str):
                bad()
            for key in ("raw_point_no", "vibration_point_num", "temperature_point_num", "bias_param_num", "velocity_param_num", "temperature_param_num"):
                if row.get(key) is not None and not isinstance(row[key], str):
                    bad()
        clean_summary = {k: summary[k] for k in SUMMARY_COUNTS if k in summary}
        complete = len(rows) == total and data.get("complete") is not False and data.get("truncated") is not True
        return cls(clean_summary, [monitoring_point(r, response["source"]) for r in rows], response["source"], complete)

    @property
    def total(self):
        return self.summary["logical_point_count"]

    def find(self, equip_num=None, point_num=None):
        rows = self.by_alias.get(point_num, []) if point_num else self.by_equipment.get(equip_num.casefold(), []) if equip_num else self.records
        return [r for r in rows if not equip_num or identity(r)[0] == equip_num.casefold()]

    def metadata(self):
        return {"total_logical_points": self.total, "returned_by_upstream": len(self.records),
                "unique_identity_count": len(self.records), "complete": self.complete, "truncated": not self.complete, "source": self.source}

    def list_result(self, q):
        scope = self.find(q.equip_num, q.point_num)
        matches, unknown = [], 0
        for row in scope:
            if ((q.equip_name and not row.get("equip_name")) or (q.point_name and not row.get("point_name"))):
                unknown += 1
                continue
            if q.equip_name and q.equip_name.casefold() not in str(row.get("equip_name") or "").casefold():
                continue
            if q.point_name and q.point_name.casefold() not in str(row.get("point_name") or "").casefold():
                continue
            if ((q.monitor_status and row.get("monitor_status") not in {"ONLINE", "OFFLINE", "SUSPENDED"})
                or (q.waveform_enabled is not None and row.get("waveform_enabled") is None)
                or (q.feature_kind and (row.get("feature_params") is None or (
                    not any(p.get("feature_kind") == q.feature_kind for p in row.get("feature_params") or [])
                    and any(not p.get("feature_kind") for p in row.get("feature_params") or []))))):
                unknown += 1
                continue
            if q.monitor_status and row.get("monitor_status") != q.monitor_status:
                continue
            if q.waveform_enabled is not None and row.get("waveform_enabled") != q.waveform_enabled:
                continue
            if q.feature_kind and not any(p.get("feature_kind") == q.feature_kind for p in row.get("feature_params") or []):
                continue
            matches.append(row)
        limited = len(matches) > q.limit
        complete = self.complete and not limited and not unknown
        warnings = []
        if not self.complete:
            warnings.append(f"监测注册表共有 {self.total} 个逻辑测点，本次上游返回 {len(self.records)} 个，来源不完整；不能据此列出全部测点或认定未命中对象未监测。")
        if limited:
            warnings.append(f"本次匹配 {len(matches)} 个逻辑测点，按返回上限展示前 {q.limit} 个。")
        if unknown:
            warnings.append(f"有 {unknown} 个测点缺少筛选所需字段，无法确认是否符合筛选条件。")
        absent = bool(q.equip_num or q.point_num) and self.complete and not scope
        status = "NOT_MONITORED" if absent else "OK" if complete else "PARTIAL"
        message = f"当前注册监测逻辑测点总数为 {self.total}；本次已取回清单中匹配 {len(matches)} 个，返回 {min(len(matches), q.limit)} 个。"
        if absent:
            message += " 完整监测注册表中没有所查询的设备或测点，该对象未纳入当前传感器自检监测范围。"
        elif not matches:
            message += " 未匹配记录不等于设备没有故障；状态、波形或参数筛选未命中也不等于该测点未监测。"
        return {"success": True, "status": status, "query_type": "MONITORED_SENSOR_POINTS", "message": message,
                "summary": {**self.summary, "total_logical_points": self.total, "returned_by_upstream": len(self.records),
                    "matched_count": len(matches), "matched_count_in_fetched": len(matches), "returned_count": min(len(matches), q.limit),
                    "complete": complete, "registry_complete": self.complete, "truncated": not self.complete,
                    "output_limited": limited, "filter_unknown_count": unknown},
                "records": matches[:q.limit], "filters": q.model_dump(exclude_none=True),
                "source": {"service": "sensor_detect_agent", "requests": [self.source], "registry": self.metadata(),
                    "truncated_possible": not self.complete, "output_limited": limited},
                "warnings": warnings, "warning": "；".join(warnings) or None}
