"""Sensor facts, identity coverage and local filters, independent of asset recall."""
from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.providers.sensor_agent import SensorError
from app.schemas.sensor import SensorQuery, MonitoredPointsQuery
from app.services.sensor_registry import SensorRegistry, point_aliases, monitoring_point

ACTIVE = {"PENDING_CONFIRMATION", "PENDING_REPAIR"}
HISTORY = {"REPAIR_COMPLETED", "AUTO_RECOVERED", "DATA_INTERRUPTED"}
FAULT_NAMES = {"PENDING_CONFIRMATION": "待确认", "PENDING_REPAIR": "待维修",
               "REPAIR_COMPLETED": "维修完成", "AUTO_RECOVERED": "自动恢复", "DATA_INTERRUPTED": "故障后数据中断"}
MONITOR_NAMES = {"ONLINE": "在线监测", "OFFLINE": "离线", "NOT_MONITORED": "未纳入监测", "UNKNOWN": "暂时无法确认"}
# Aliases select an entry from the LIVE dictionary; they never create rule facts.
TYPE_ALIASES = {"偏置电压": "BIAS_VOLTAGE_ABNORMAL", "偏执电压异常": "BIAS_VOLTAGE_ABNORMAL",
    "偏执电压": "BIAS_VOLTAGE_ABNORMAL", "偏置异常": "BIAS_VOLTAGE_ABNORMAL",
    "速度有效值": "VELOCITY_RMS_ABNORMAL", "温度": "TEMPERATURE_ABNORMAL",
    "卡死": "CONSTANT_VALUE", "数据恒定": "CONSTANT_VALUE", "毛刺": "SPIKE",
    "削顶": "WAVEFORM_CLIPPING", "偏度异常": "SKEWNESS_ABNORMAL", "歪度": "SKEWNESS_ABNORMAL"}


def ident(row):
    return str(row.get("equip_num") or "").casefold(), str(row.get("point_num") or "")


def aliases(row):
    return point_aliases(row)


def valid_records(response, key="records"):
    data = response["body"].get("data")
    records = data.get(key) if isinstance(data, dict) else None
    if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
        raise SensorError("SENSOR_AGENT_BAD_RESPONSE", "传感器接口的记录列表结构异常，无法将它解释为空结果。", path=response["source"]["endpoint"])
    return records


class SensorQueryService:
    def __init__(self, settings, client, points, catalog=None):
        self.settings, self.client, self.points = settings, client, points
        from app.services.sensor_identity_index import SensorIdentityIndex
        self.catalog = catalog
        self.identity_index = SensorIdentityIndex(client, settings.sensor_agent_registry_mapping_ttl_seconds)

    async def close(self):
        await self.identity_index.close()

    async def resolve_identity(self, tokens, *, refresh=False):
        from app.services.sensor_identity_index import resolve_tokens
        from app.services.entity_resolver import candidate_to_entity
        if not tokens or len(tokens) > 32 or any(not isinstance(x, str) or not x.strip() or len(x) > 256 for x in tokens):
            raise SensorError("INVALID_QUERY_PARAMETER", "待核验编码格式不正确。")
        tokens = list(dict.fromkeys(tokens))
        async def catalog_lookup():
            return await self.catalog.lookup_code_tokens(tokens) if self.catalog else []
        snapshot, catalog = await asyncio.gather(self.identity_index.get(refresh=refresh), catalog_lookup(), return_exceptions=True)
        result = {"targets": [], "input_tokens": tokens, "absence_verified": False, "asset_entities": []}
        if isinstance(snapshot, Exception):
            result["registry_error"] = snapshot.payload()["error"] if isinstance(snapshot, SensorError) else {"code": "SENSOR_AGENT_UNAVAILABLE"}
        else:
            registry, error = snapshot
            result.update(resolve_tokens(registry, tokens, stale=bool(error)))
            # A five-minute mapping cache is positive-only. Confirm misses against
            # the live (ten-second) source before treating absence as established.
            age = self.timestamp(registry.source.get("fetched_at"))
            old = age is None or (datetime.now(self.zone())-age).total_seconds() > self.settings.sensor_agent_cache_ttl_seconds
            if not result["targets"] and not error and old:
                try:
                    registry, error = await self.identity_index.get(refresh=True)
                    result.update(resolve_tokens(registry, tokens, stale=bool(error)))
                except SensorError as exc:
                    error = exc.payload()["error"]
                    result["absence_verified"] = False
            if not result["targets"] and not registry.complete and self.identity_index.last_complete:
                previous = resolve_tokens(self.identity_index.last_complete, tokens, stale=True)
                if previous["targets"]:
                    result.update(targets=previous["targets"], mapping_stale=True,
                                  match_count=previous["match_count"], output_limited=previous["output_limited"],
                                  previous_mapping_source=previous["registry"]["source"], absence_verified=False)
            if error:
                result["registry_error"] = error
        if isinstance(catalog, Exception):
            result["asset_error"] = {"code": "ASSET_LOOKUP_UNAVAILABLE"}
        else:
            # A point in the registry can reveal its equipment without pretending
            # that this logical point exists in the physical asset catalog.
            extra_codes = [t["equip_num"] for t in result["targets"] if t["equip_num"] not in tokens]
            if len(result["targets"]) == 1:
                extra_codes += [x for x in result["targets"][0].get("aliases") or [] if x not in tokens]
            extra_codes = list(dict.fromkeys(extra_codes))
            if extra_codes and self.catalog:
                try:
                    catalog += await self.catalog.lookup_code_tokens(extra_codes[:32])
                except Exception:
                    result["asset_error"] = {"code": "ASSET_LOOKUP_UNAVAILABLE"}
            unique = {(c.entity_type, c.entity_key, c.equip_no, c.point_no): c for c in catalog}
            result["asset_entities"] = [candidate_to_entity(c).model_dump(mode="json", exclude_none=True) for c in list(unique.values())[:100]]
            result["asset_output_limited"] = len(unique) > 100
        return {"success": True, "status": "OK" if result["targets"] else "PARTIAL",
                "query_type": "SENSOR_IDENTITY", "identity_resolution": result,
                "message": "已核验传感器编码对应关系；在线状态与故障状态需单独查询。"}

    def zone(self):
        try:
            return ZoneInfo(self.settings.sensor_agent_timezone)
        except (KeyError, ValueError) as exc:
            raise SensorError("SENSOR_AGENT_NOT_CONFIGURED", "传感器服务时区配置不正确。") from exc

    def timestamp(self, raw):
        if raw in (None, ""):
            return None
        try:
            d = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=self.zone())
        except ValueError:
            return None

    def time_text(self, raw):
        value = self.timestamp(raw)
        return value.isoformat() if value else None

    async def _enrich_asset_identity(self, *groups):
        """Best-effort exact-code enrichment; never changes sensor query success.

        Sensor Agent owns monitoring/fault facts.  The asset catalog only fills
        human-readable identity fields for already returned authoritative codes.
        """
        rows = [row for group in groups for row in (group or []) if isinstance(row, dict)]
        if not rows or self.catalog is None:
            return {"attempted": False, "enriched_count": 0, "missing_count": 0}
        tokens = []
        for row in rows:
            for key in ("point_num", "point_no", "equip_num", "equip_no"):
                value = str(row.get(key) or "").strip()
                if value and value.upper() not in {x.upper() for x in tokens}:
                    tokens.append(value)
        if not tokens:
            return {"attempted": False, "enriched_count": 0, "missing_count": 0}
        try:
            batches = [tokens[i:i+32] for i in range(0, len(tokens), 32)]
            results = await asyncio.gather(*(self.catalog.lookup_code_tokens(batch) for batch in batches))
            candidates = [candidate for batch in results for candidate in batch]
        except Exception:
            return {"attempted": True, "enriched_count": 0, "missing_count": len(rows), "error": "ASSET_LOOKUP_UNAVAILABLE"}

        point_map = {}
        point_by_no = {}
        equipment_map = {}
        for candidate in candidates:
            equip = str(candidate.equip_no or "").casefold()
            point = str(candidate.point_no or "").casefold()
            if candidate.entity_type == "point" and point:
                point_map[(equip, point)] = candidate
                point_by_no.setdefault(point, []).append(candidate)
            elif candidate.entity_type == "equipment" and equip:
                equipment_map[equip] = candidate

        enriched = 0
        unresolved = 0
        for row in rows:
            equip = str(row.get("equip_num") or row.get("equip_no") or "").casefold()
            point = str(row.get("point_num") or row.get("point_no") or "").casefold()
            candidate = point_map.get((equip, point)) if point else None
            if candidate is None and point and len(point_by_no.get(point, [])) == 1:
                candidate = point_by_no[point][0]
            equipment = equipment_map.get(equip)
            if candidate is None and equipment is None:
                unresolved += 1
                continue
            before = (row.get("point_name"), row.get("equip_name"), row.get("space_path"))
            if candidate is not None:
                meta = candidate.metadata if isinstance(candidate.metadata, dict) else {}
                if not row.get("point_name"):
                    row["point_name"] = meta.get("point_name") or candidate.display_name or None
                if not row.get("equip_name"):
                    row["equip_name"] = meta.get("equip_name") or None
                if not row.get("space_path"):
                    row["space_path"] = meta.get("space_path") or None
            if equipment is not None:
                meta = equipment.metadata if isinstance(equipment.metadata, dict) else {}
                if not row.get("equip_name"):
                    row["equip_name"] = meta.get("equip_name") or equipment.display_name or None
                if not row.get("space_path"):
                    row["space_path"] = meta.get("space_path") or None
            after = (row.get("point_name"), row.get("equip_name"), row.get("space_path"))
            if after != before:
                enriched += 1
        return {"attempted": True, "enriched_count": enriched, "missing_count": unresolved}

    async def monitored_points(self, q: MonitoredPointsQuery):
        await self.client.ready(refresh=q.refresh)
        registry = SensorRegistry.parse(await self.client.monitored_points(refresh=q.refresh))
        result = registry.list_result(q)
        result["source"]["cache_max_age_seconds"] = self.settings.sensor_agent_cache_ttl_seconds
        enrichment = await self._enrich_asset_identity(result.get("records") or [])
        result["source"]["asset_identity_enrichment"] = enrichment
        if enrichment.get("error"):
            result.setdefault("warnings", []).append("资产目录暂时无法补充部分测点名称；传感器监测结果本身仍有效。")
        elif enrichment.get("missing_count"):
            result.setdefault("warnings", []).append(
                f"有 {enrichment['missing_count']} 条传感器记录未在资产目录中补到名称/归属信息；保留真实编码展示。"
            )
        return result

    async def coverage(self, q, dashboard, fault_rows=(), registry=None):
        if registry is None or not q.equip_num:
            result = await self._legacy_coverage(q, dashboard, fault_rows)
        else:
            rows = registry.find(q.equip_num, q.point_num)
            # Do not choose arbitrarily if an observed parameter/physical alias is
            # attached to more than one registered logical point.
            ambiguous = bool(q.point_num and len(rows) > 1)
            if (q.point_num and rows) or registry.complete:
                if not rows and q.point_num:
                    rows = [{"equip_num": q.equip_num, "point_num": q.point_num, "monitored": False,
                             "monitoring_status": "NOT_MONITORED", "monitoring_status_name": "未纳入监测",
                             "evidence_source": "sensor_registry.complete_absence"}]
                result = self._summarize_coverage(q, rows, not ambiguous,
                    absent=not rows, equipment_name=rows[0].get("equip_name") if rows else None)
                result["asset_catalog_checked"] = False
                result["scope_source"] = "sensor_registry"
                if ambiguous:
                    result["message"] = "该测点编码对应多个逻辑测点，尚未唯一核实；请核对所属逻辑测点。"
                # Different endpoints need not share one transaction snapshot.
                # A contradictory positive cannot establish not-monitored/online.
                off = [r for r in self._offline(dashboard) if ident(r)[0] == q.equip_num.casefold()
                       and (not q.point_num or q.point_num in aliases(r))]
                conflict = any(not any(ident(r) == ident(p) and p["monitoring_status"] == "OFFLINE" for p in rows) for r in off)
                if conflict:
                    uncertain = [{**p, "monitored": True if p.get("monitored") is True else None,
                                  "monitoring_status": "UNKNOWN", "monitoring_status_name": "两个接口状态不一致，暂时无法确认"} for p in rows]
                    result.update(self._summarize_coverage(q, uncertain, False, equipment_name=result.get("equip_name")))
                    result.update(status="PARTIAL", complete=False, source_conflict=True,
                        message="监测注册表与离线看板对该范围的记录不一致，可能在不同快照间发生变化；暂不能确认该范围的完整状态或认定未监测。")
            else:
                # A partial registry is not a negative membership index.
                # Reuse positive rows, then retain bounded point verification for
                # a specifically named target; never expand list queries to N+1.
                result = await self._legacy_coverage(q, dashboard, fault_rows, registry=registry)
                result["scope_source"] = "registry_and_bounded_point_verification"
                if not q.point_num:
                    result.update(complete=False, status="PARTIAL",
                        message="监测注册表本次未返回完整清单，已结合资产目录核实部分测点；不能保证列出该设备全部受监测测点或判断整台设备全部在线。")
        if registry:
            result["registry"] = registry.metadata()
            if not q.equip_num:
                result["registry_total_logical_points"] = registry.total
                if result.get("configured_sensor_count") != registry.total:
                    result.update(complete=False, source_conflict=True,
                        message="注册表与看板的总数未能核对一致，本次分别保留两方统计，不混合推导完整状态。")
        return result

    def _offline(self, dashboard):
        rows = valid_records(dashboard, "offline_sensor_list")
        if any(not r.get("equip_num") or not r.get("point_num") or r.get("monitor_status") == "ONLINE" for r in rows):
            raise SensorError("SENSOR_AGENT_BAD_RESPONSE", "传感器离线列表包含缺少身份或状态矛盾的记录，无法确认完整状态。")
        return rows

    @staticmethod
    def _offline_point(row):
        result = monitoring_point(row, {}, evidence="dashboard.offline_sensor_list")
        result["monitoring_status"] = "OFFLINE"
        if row.get("monitor_status") not in {"OFFLINE", "SUSPENDED"}:
            result["monitoring_status_name"] = "离线，当前没有新鲜数据"
        return result

    async def _point(self, equip, point, offline, refresh):
        matched = [r for r in offline if ident(r)[0] == equip.casefold() and point in aliases(r)]
        if len({ident(r) for r in matched}) == 1:
            return self._offline_point(matched[0])
        base = {"equip_num": equip, "point_num": point, "monitored": None,
                "monitoring_status": "UNKNOWN", "monitoring_status_name": "暂时无法确认"}
        try:
            result = await self.client.point_state(equip, point, refresh=refresh)
        except SensorError as exc:
            if exc.code == "POINT_NOT_MONITORED":
                return {**base, "monitored": False, "monitoring_status": "NOT_MONITORED",
                        "monitoring_status_name": "未纳入监测", "message": exc.message,
                        "evidence_source": "point_state.explicit_not_found"}
            return {**base, "error_code": exc.code, "message": exc.message}
        data = result["body"].get("data")
        p = data.get("point") if isinstance(data, dict) else None
        if not isinstance(p, dict) or str(p.get("equip_no") or "").casefold() != equip.casefold() or point not in aliases(p) or not p.get("point_no"):
            return {**base, "error_code": "SENSOR_IDENTITY_MISMATCH", "message": "测点状态响应未确认所查询的设备和测点，暂不能判断是否纳入监测。"}
        return monitoring_point({**p, "monitor_status": data.get("monitor_status")}, result["source"], evidence="point_state")

    async def _legacy_coverage(self, q, dashboard, fault_rows=(), registry=None):
        offline = self._offline(dashboard)
        data = dashboard["body"]["data"]
        if not q.equip_num:
            count = data.get("offline_sensor_count")
            count_matches = isinstance(count, int) and not isinstance(count, bool) and count == len(offline)
            return {"scope": "global", "status": "GLOBAL_SUMMARY", "complete": count_matches,
                    "configured_sensor_count": data.get("configured_sensor_count"),
                    "online_sensor_count": data.get("monitored_sensor_count"),
                    "offline_sensor_count": data.get("offline_sensor_count"),
                    "offline_list_count_matches": count_matches,
                    "message": "统计范围为传感器自检服务已配置的逻辑测点，不代表资产平台所有测点均已纳入监测。" +
                        ("" if count_matches else "上游离线总数与返回列表未能核对一致，结果可能不完整。"), "records": []}
        catalog_truncated = False
        catalog_count = None
        equipment_name = None
        if q.point_num:
            records = [await self._point(q.equip_num, q.point_num, offline, q.refresh)]
            if records[0].get("monitored") is False:
                # The caller may have an authoritative ASSET temperature/raw point
                # code while Sensor Agent accepts only its logical code. Resolve
                # only mappings actually returned for the device's catalog points.
                device = await self.coverage(SensorQuery(equip_num=q.equip_num, refresh=q.refresh), dashboard, fault_rows, registry=registry)
                mapped = [p for p in device["records"] if p.get("monitored") is True and q.point_num in aliases(p)]
                if len({ident(p) for p in mapped}) == 1:
                    records = [mapped[0]]
                elif not device["complete"]:
                    records[0].update(monitored=None, monitoring_status="UNKNOWN", monitoring_status_name="暂时无法确认",
                                      message="未找到直接对应的逻辑测点，且设备测点映射尚未核对完整，暂不能认定该测点未监测。")
        else:
            equipment, points, catalog_truncated = await self.points.query(equip_no=q.equip_num, point_type=None, keyword=None,
                                                                          limit=self.settings.sensor_agent_scope_max_points)
            equipment_name = equipment.get("equip_name") or equipment.get("display_name")
            catalog_count = len(points)
            records = [self._offline_point(r) for r in offline if ident(r)[0] == q.equip_num.casefold()]
            if registry:
                records.extend(registry.find(q.equip_num))
                records = list({ident(r): r for r in records}.values())
            known_aliases = set().union(*(aliases(r) for r in records)) if records else set()
            # Codes come from the asset catalog or observed fault rows, never suffix inference.
            point_codes = list(dict.fromkeys([str(p["point_no"]) for p in points if p.get("point_no")]
                              + [str(r["point_num"]) for r in fault_rows if ident(r)[0] == q.equip_num.casefold() and r.get("point_num")]))
            remaining = [p for p in point_codes if p not in known_aliases]
            maximum = self.settings.sensor_agent_scope_max_points
            catalog_truncated = catalog_truncated or len(remaining) > maximum
            codes = remaining[:maximum]
            answers = {}
            cursor = 0
            async def worker():
                nonlocal cursor
                while cursor < len(codes):
                    p = codes[cursor]
                    cursor += 1
                    answers[p] = await self._point(q.equip_num, p, offline, q.refresh)
            # Schedule only a fixed worker set: timing out a large scope must not
            # leave hundreds of shielded cache factories queued in the background.
            tasks = [asyncio.create_task(worker()) for _ in range(min(len(codes), self.settings.sensor_agent_max_parallel))]
            if tasks:
                try:
                    done, pending = await asyncio.wait(tasks, timeout=self.settings.sensor_agent_scope_timeout_seconds)
                    for task in done:
                        task.result()
                    for task in pending:
                        task.cancel()
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                for p in codes:
                    records.append(answers.get(p) or {"equip_num": q.equip_num, "point_num": p, "monitored": None,
                        "monitoring_status": "UNKNOWN", "monitoring_status_name": "暂时无法确认", "message": "设备范围状态核对达到时间预算。"})
        # An explicit mapping returned by another point request can resolve an A/T
        # catalog alias that the logical-only endpoint rejected. Never strip suffixes.
        positives = [r for r in records if r.get("monitored") is True]
        resolved = []
        for r in records:
            matches = [p for p in positives if ident(p)[0] == ident(r)[0] and r.get("point_num") in aliases(p)]
            resolved.append(matches[0] if len({ident(p) for p in matches}) == 1 else r)
        unique = {ident(r): r for r in resolved}
        records = sorted(unique.values(), key=lambda r: (r.get("point_name") or "", r.get("point_num") or ""))
        return self._summarize_coverage(q, records, not catalog_truncated, catalog_truncated=catalog_truncated,
                                        catalog_count=catalog_count, equipment_name=equipment_name)

    @staticmethod
    def _summarize_coverage(q, records, complete, *, absent=False, catalog_truncated=False, catalog_count=None, equipment_name=None):
        counts = Counter(r["monitoring_status"] for r in records)
        complete = complete and counts["UNKNOWN"] == 0
        if absent and complete:
            status = "NOT_MONITORED"
        elif not records:
            status = "UNKNOWN"
        elif all(r.get("monitored") is False for r in records) and complete:
            status = "NOT_MONITORED"
        elif not complete:
            status = "PARTIAL"
        elif counts["ONLINE"] and counts["OFFLINE"]:
            status = "MIXED"
        elif counts["ONLINE"]:
            status = "ONLINE"
        elif counts["OFFLINE"]:
            status = "OFFLINE"
        else:
            status = "UNKNOWN"
        message = {"NOT_MONITORED": "所查询的设备或测点未纳入当前传感器自检服务的监测范围，不能把它解释为在线或没有故障。",
                   "UNKNOWN": "现有接口和资产目录不足以确认该对象的监测范围。",
                   "PARTIAL": "已核实部分测点状态，仍有测点未确认或未取全，不能据此判断整台设备全部在线。",
                   "MIXED": "该设备已核实的传感器中同时存在在线和离线测点。",
                   "ONLINE": "已核实的受监测测点当前在线；在线不等同于设备正在运行或设备无故障。",
                   "OFFLINE": "已核实的受监测测点当前离线；离线不等同于设备停机。"}[status]
        if counts["NOT_MONITORED"] and status != "NOT_MONITORED":
            message += f" 另有 {counts['NOT_MONITORED']} 个目录测点未纳入监测。"
        return {"scope": "point" if q.point_num else "equipment", "equip_num": q.equip_num, "equip_name": equipment_name,
                "status": status, "complete": complete, "catalog_truncated": catalog_truncated,
                "catalog_points_fetched": catalog_count, "checked_logical_points": len(records),
                "monitored_point_count": sum(r.get("monitored") is True for r in records),
                "online_point_count": counts["ONLINE"], "offline_point_count": counts["OFFLINE"],
                "unmonitored_point_count": counts["NOT_MONITORED"], "unknown_point_count": counts["UNKNOWN"],
                "records": records, "message": message}

    @staticmethod
    def _rules(response):
        rows = valid_records(response)
        if any(not r.get("rule_code") or not r.get("model_name") for r in rows):
            raise SensorError("SENSOR_AGENT_BAD_RESPONSE", "传感器故障类型字典结构不完整。")
        return rows

    @staticmethod
    def _resolve_type(name, rules):
        if not name:
            return None
        wanted = TYPE_ALIASES.get(name, name).casefold()
        exact = [r for r in rules if wanted in {str(r.get("rule_code")).casefold(), str(r.get("model_name")).casefold()}]
        matches = exact or [r for r in rules if wanted in str(r.get("model_name") or "").casefold()]
        by_code = {r["rule_code"]: r for r in matches}
        if len(by_code) != 1:
            names = "、".join(str(r["model_name"]) for r in (matches or rules))
            raise SensorError("SENSOR_FAULT_TYPE_AMBIGUOUS" if matches else "SENSOR_FAULT_TYPE_UNSUPPORTED",
                              "未能唯一确认所查询的传感器故障类型。当前可查询类型："+names)
        return next(iter(by_code.values()))

    @staticmethod
    def _rule_for(row, rules):
        if row.get("rule_code"):
            return row["rule_code"], "fault_record"
        matches = {r["rule_code"] for r in rules if str(r.get("model_type")) == str(row.get("model_type")) and r.get("model_name") == row.get("model_name")}
        return (next(iter(matches)), "live_rule_dictionary") if len(matches) == 1 else (None, None)

    def _fault_record(self, row, rules, include_analysis, *, detail=False):
        code, source = self._rule_for(row, rules)
        result = {"fault_id": row.get("fault_id") or row.get("id"), "equip_num": row.get("equip_num"), "point_num": row.get("point_num"),
                  "point_name": row.get("point_name"), "param_num": row.get("param_num"),
                  "fault_type_name": row.get("model_name"), "model_type": row.get("model_type"),
                  "rule_code": code, "rule_code_source": source, "fault_status": row.get("fault_status"),
                  "fault_status_name": row.get("fault_status_name") or FAULT_NAMES.get(row.get("fault_status")),
                  "start_time": self.time_text(row.get("start_time")), "end_time": self.time_text(row.get("end_time")),
                  "monitor_status_name": row.get("monitor_status_name"), "unit": row.get("unit")}
        if include_analysis:
            analysis = str(row.get("analysis_result") or "")
            result["analysis_result"] = analysis if detail else analysis[:6000]
            result["analysis_complete"] = detail or len(analysis) <= 6000
            result["analysis_char_count"] = len(analysis)
            result["remark"] = str(row.get("remark") or "")[:1500]
        return result

    async def query(self, kind: str, q: SensorQuery):
        await self.client.ready(refresh=q.refresh)
        needs_faults = kind in {"active", "history", "overview"}
        allowed = HISTORY if kind == "history" else ACTIVE
        if q.fault_status and (not needs_faults or q.fault_status not in allowed):
            raise SensorError("INVALID_QUERY_PARAMETER", "故障处理状态与当前查询范围不一致。")
        if kind not in {"history"} and any(getattr(q, k) for k in ("start_time_from", "start_time_to", "end_time_from", "end_time_to")):
            raise SensorError("INVALID_QUERY_PARAMETER", "时间范围筛选请使用传感器历史故障查询。")
        for stem in ("start_time", "end_time"):
            lo, hi = self.timestamp(getattr(q, stem+"_from")), self.timestamp(getattr(q, stem+"_to"))
            if lo and hi and lo >= hi:
                raise SensorError("INVALID_QUERY_PARAMETER", "时间范围开始值必须早于结束值。")
        requests = [self.client.dashboard(refresh=q.refresh)]
        if needs_faults:
            requests.append(self.client.faults("history" if kind == "history" else "active", refresh=q.refresh))
        if q.fault_type:
            requests.append(self.client.rules(refresh=q.refresh))
        # Optional for old tools: a missing newly documented endpoint must not
        # discard otherwise usable existing fault/state responses. The new list
        # tool itself is strict and returns its upstream error directly.
        registry_index = len(requests) if q.equip_num or kind in {"monitoring", "overview"} else None
        if registry_index is not None:
            requests.append(self.client.monitored_points(refresh=q.refresh))
        fetched = await asyncio.gather(*requests, return_exceptions=True)
        registry = None
        registry_error = None
        if registry_index is not None:
            value = fetched.pop(registry_index)
            try:
                if isinstance(value, BaseException):
                    raise value
                registry = SensorRegistry.parse(value)
            except SensorError as exc:
                registry_error = exc.payload()
        for value in fetched:
            if isinstance(value, BaseException):
                raise value
        dashboard = fetched[0]
        fault_response = fetched[1] if needs_faults else None
        rules = self._rules(fetched[-1]) if q.fault_type else []
        fault_rows = valid_records(fault_response) if fault_response else []
        try:
            rule = self._resolve_type(q.fault_type, rules)
        except SensorError as exc:
            if exc.code != "SENSOR_FAULT_TYPE_UNSUPPORTED" or not q.fault_type:
                raise
            # A stale rules cache cannot turn a new configured type into a false
            # negative. Refresh once; historical original names remain searchable.
            refreshed = await self.client.rules(refresh=True)
            rules = self._rules(refreshed)
            fetched.append(refreshed)
            try:
                rule = self._resolve_type(q.fault_type, rules)
            except SensorError:
                if any(r.get("model_name") == q.fault_type for r in fault_rows):
                    rule = {"rule_code": None, "model_name": q.fault_type, "enabled": None}
                else:
                    raise
        coverage = await self.coverage(q, dashboard, fault_rows, registry=registry)
        warnings = []
        if registry_error:
            warnings.append("监测注册表暂不可用，已按原有接口核对本次查询；不能提供完整监测清单。")
        if not coverage["complete"]:
            warnings.append(coverage["message"])
        sources = [r["source"] for r in fetched]
        if registry:
            sources.append(registry.source)
        result = {"success": True, "status": "OK", "query_type": kind.upper(), "records": [],
                  "coverage": coverage, "filters": q.model_dump(exclude_none=True), "warnings": warnings,
                  "source": {"service": "sensor_detect_agent", "requests": sources,
                             "cache_max_age_seconds": self.settings.sensor_agent_cache_ttl_seconds,
                             "timezone": self.settings.sensor_agent_timezone,
                             "snapshot_time": self.time_text(dashboard["body"]["data"].get("snapshot_time"))}}
        if registry:
            result["source"]["registry"] = registry.metadata()
        if registry_error:
            result["source"]["registry_error"] = registry_error["error"]
        if needs_faults:
            if any(not r.get("equip_num") or not r.get("point_num") or str(r.get("fault_status")) not in allowed for r in fault_rows):
                raise SensorError("SENSOR_AGENT_BAD_RESPONSE", "传感器故障列表包含缺少身份或不属于所查询状态的记录，无法确认完整结果。")
            ids = [str(r.get("id")) for r in fault_rows]
            duplicated = len(ids)-len(set(ids))
            if duplicated:
                warnings.append(f"上游返回记录中有 {duplicated} 条复用故障编号；保留原始记录，不按编号合并不同历史事件。")
            truncated = len(fault_rows) >= self.settings.sensor_agent_fetch_limit
            source_data = fault_response["body"]["data"]
            total = source_data.get("total", source_data.get("total_count"))
            if total is not None and (type(total) is not int or total < len(fault_rows)):
                raise SensorError("SENSOR_AGENT_BAD_RESPONSE", "故障列表的总数与实际记录无法核对。")
            source_complete = (len(fault_rows) == total if total is not None else None)
            if source_data.get("complete") is False or source_data.get("truncated") is True:
                source_complete = False
            if source_complete is False:
                truncated = True
            result["source"].update(complete=source_complete, requested_limit=self.settings.sensor_agent_fetch_limit,
                                    upstream_total=total)
            if source_complete is None:
                warnings.append("故障接口未提供可核验的总数或完整性声明，以下结论仅覆盖本次取回的记录。")
            if truncated:
                warnings.append(f"故障接口本次返回数量触及请求上限 {self.settings.sensor_agent_fetch_limit} 条，结果可能不完整。")
            logical = {r["point_num"] for r in coverage["records"] if q.point_num and r.get("monitored") is True}
            if q.point_num:
                logical.add(q.point_num)
            matched, bad_times, simulations, unresolved_types = [], 0, 0, 0
            for row in fault_rows:
                if row.get("source_type") == "SIMULATION" or row.get("simulation_id"):
                    simulations += 1
                    continue
                if q.equip_num and ident(row)[0] != q.equip_num.casefold():
                    continue
                if q.point_num and row.get("point_num") not in logical:
                    continue
                if q.fault_status and row.get("fault_status") != q.fault_status:
                    continue
                if rule:
                    code, _ = self._rule_for(row, rules)
                    if not (code and code == rule["rule_code"]) and row.get("model_name") != rule["model_name"]:
                        if rule.get("model_type") is not None and str(row.get("model_type")) == str(rule["model_type"]):
                            unresolved_types += 1
                        continue
                keep = True
                for stem in ("start_time", "end_time"):
                    lo, hi = self.timestamp(getattr(q, stem+"_from")), self.timestamp(getattr(q, stem+"_to"))
                    if lo or hi:
                        actual = self.timestamp(row.get(stem))
                        if actual is None:
                            bad_times += 1
                            keep = False
                        elif (lo and actual < lo) or (hi and actual >= hi):
                            keep = False
                if keep:
                    matched.append(row)
            if bad_times:
                warnings.append("部分记录时间无法解析，时间筛选结果可能不完整。")
            if unresolved_types:
                warnings.append("部分记录的故障类型名称与当前规则字典不一致，无法可靠确认是否属于所查规则；筛选结果可能不完整。")
            if rule and rule.get("enabled") is False:
                warnings.append("当前该传感器故障检测规则未启用，不能根据没有新故障记录判断检测正常；已有历史记录仍可查询。")
            if rule and rule["rule_code"] == "BIAS_VOLTAGE_ABNORMAL" and any(r.get("bias_input_configured") is False for r in coverage["records"]):
                warnings.append("部分已核对测点未配置偏置电压检测输入，不能把这些测点没有偏置电压故障记录解释为已检测正常。")
            simulation_verified = all("source_type" in r or "simulation_id" in r for r in fault_rows)
            if not simulation_verified:
                warnings.append("正式故障列表未完整标注模拟来源，接口也未提供模拟数据排除参数；本次无法独立验证所有故障记录均为实际数据。")
            result["records"] = [self._fault_record(r, rules, q.include_analysis) for r in matched[:q.limit]]
            result["summary"] = {"matched_count_in_fetched": len(matched), "returned_count": min(len(matched), q.limit),
                "unique_point_count_in_matched": len({ident(r) for r in matched}),
                "unique_equipment_count_in_matched": len({ident(r)[0] for r in matched}),
                "published_fault_found": bool(matched),
                "fault_type": rule["model_name"] if rule else q.fault_type,
                "rule_enabled_now": rule.get("enabled") if rule else None}
            result["source"].update(upstream_record_count=len(fault_rows), duplicate_fault_id_count=duplicated,
                truncated_possible=truncated, output_limited=len(matched)>q.limit, simulation_exclusion_verified=simulation_verified,
                explicitly_simulated_records_excluded=simulations, time_filter_unparseable_count=bad_times)
            result["source"]["unresolved_fault_type_count"] = unresolved_types
            result["message"] = f"在本次返回的{'历史' if kind == 'history' else '当前正式'}故障中，匹配到 {len(matched)} 条{rule['model_name'] if rule else '传感器故障'}记录。"
            if not matched:
                result["message"] += " 未查到记录不等同于测点已监测、传感器在线或设备没有故障。"
        else:
            if not q.equip_num:
                rows = [self._offline_point(r) for r in self._offline(dashboard)] if kind == "offline" else []
            else:
                rows = [r for r in coverage["records"] if kind != "offline" or r["monitoring_status"] == "OFFLINE"]
            result["records"] = rows[:q.limit]
            result["summary"] = {"matched_count_in_fetched": len(rows), "returned_count": min(len(rows), q.limit)}
            result["source"].update(output_limited=len(rows)>q.limit, truncated_possible=not coverage["complete"])
            result["message"] = coverage["message"]
        if kind == "overview":
            result["monitoring_summary"] = {k:v for k,v in coverage.items() if k != "records"}
            result["offline_sensors"] = [r for r in coverage["records"] if r["monitoring_status"] == "OFFLINE"][:q.limit]
            if not q.equip_num:
                result["offline_sensors"] = [self._offline_point(r) for r in self._offline(dashboard)[:q.limit]]
        if q.equip_num:
            result["message"] += " " + coverage["message"] if needs_faults else ""
        if coverage["status"] == "NOT_MONITORED":
            result["status"] = "NOT_MONITORED"
        elif not coverage["complete"] or coverage["status"] in {"PARTIAL", "UNKNOWN"} or result["source"].get("truncated_possible") or result["source"].get("time_filter_unparseable_count") or result["source"].get("unresolved_fault_type_count"):
            result["status"] = "PARTIAL"
        all_coverage = coverage["records"]
        coverage["returned_count"] = min(len(all_coverage), q.limit)
        coverage["output_limited"] = len(all_coverage) > q.limit
        coverage["unmonitored_samples"] = [r for r in all_coverage if r.get("monitored") is False][:20]
        coverage["unknown_samples"] = [r for r in all_coverage if r.get("monitoring_status") == "UNKNOWN"][:20]
        coverage["records"] = all_coverage[:q.limit]
        enrichment = await self._enrich_asset_identity(
            result.get("records") or [], coverage.get("records") or [], result.get("offline_sensors") or []
        )
        result["source"]["asset_identity_enrichment"] = enrichment
        if enrichment.get("error"):
            result["warnings"].append("资产目录暂时无法补充部分测点名称；传感器故障/监测结果本身仍有效。")
        elif enrichment.get("missing_count"):
            result["warnings"].append(
                f"有 {enrichment['missing_count']} 条传感器记录未在资产目录中补到名称/归属信息；保留真实编码展示。"
            )
        return result

    async def fault_evidence(self, fault_id: str, equip_num: str, point_num: str | None = None, *, refresh=False):
        await self.client.ready(refresh=refresh)
        response = await self.client.evidence(fault_id, refresh=refresh)
        row = response["body"].get("data")
        if not isinstance(row, dict) or str(row.get("equip_num") or "").casefold() != equip_num.casefold() or str(row.get("fault_id") or row.get("id") or "") != fault_id:
            raise SensorError("SENSOR_IDENTITY_MISMATCH", "故障证据与本轮确认的设备或故障编号不一致，已停止使用该结果。")
        if point_num and row.get("point_num") != point_num:
            raise SensorError("SENSOR_IDENTITY_MISMATCH", "故障证据不属于当前确认的逻辑测点。")
        record = self._fault_record(row, [], True, detail=True)
        latest = row.get("latest_data") if isinstance(row.get("latest_data"), dict) else {}
        record["sensor"] = {k:v for k,v in (latest.get("sensor") or {}).items() if k in {"point_name", "logical_point_num", "vibration_point_num", "temperature_point_num"}}
        return {"success": True, "status": "OK", "query_type": "FAULT_EVIDENCE", "record": record,
                "source": response["source"], "message": "已取得指定正式故障的证据；故障记录中的分析反映该次故障，不代表已经重新执行实时诊断。"}
