from __future__ import annotations

from datetime import timedelta
import json
from typing import Any

from app.config import Settings
from app.encoding import bytes_from_mongo_binary, encode_large_json, encode_waveform
from app.repositories.mongo import MongoRepository
from app.time_utils import default_trend_range, now, parse_time, to_iso


class DataService:
    def __init__(self, settings: Settings, mongo: MongoRepository):
        self.settings = settings
        self.mongo = mongo

    def get_waveform(self, device_code: str, point_no: str, target_time: str | None = None, mode: str = "latest", search_window_seconds: int = 86400) -> dict[str, Any]:
        if mode not in {"latest", "nearest", "latest_before"}:
            raise ValueError("mode只支持latest、nearest、latest_before")
        if mode != "latest" and not target_time:
            raise ValueError(f"mode={mode}时必须提供target_time")
        record = self.mongo.get_waveform(device_code, point_no, mode, target_time, max(1, search_window_seconds))
        if not record:
            return {"success": True, "data": None}
        raw = bytes_from_mongo_binary(record["raw"])
        sample_count = len(raw) // 4
        if sample_count > self.settings.waveform_max_samples:
            raise ValueError(f"波形点数{sample_count}超过上限{self.settings.waveform_max_samples}")
        encoded, decode = encode_waveform(raw)
        if len(encoded) > self.settings.max_response_bytes:
            raise ValueError("波形响应超过MCP_MAX_RESPONSE_BYTES，请缩小查询范围或调整服务上限")
        data = {
            "point_no": point_no,
            "resolved_time": record["resolved_time"],
            "sample_rate_hz": record["sample_rate_hz"],
            "sample_count": sample_count,
            "values_base64": encoded,
        }
        if record.get("offset_seconds") is not None:
            data["offset_seconds"] = record["offset_seconds"]
        return {"success": True, "data": data, "decode": decode}

    def _trend_response(self, point_id: str, start_time: str, end_time: str, series, return_mode: str) -> dict[str, Any]:
        serialized = [item.to_dict() for item in series]
        point_count = sum(len(item["samples"]) for item in serialized)
        if point_count > self.settings.trend_max_points:
            raise ValueError(f"趋势点数{point_count}超过上限{self.settings.trend_max_points}，请缩小时间范围")
        use_binary = return_mode == "binary" or (return_mode == "auto" and point_count > self.settings.trend_inline_max_points)
        base = {"point_id": point_id, "start_time": start_time, "end_time": end_time, "point_count": point_count}
        if not use_binary:
            base["series"] = serialized
            return {"success": True, "data": base, "decode": {"required": False}}
        payload, decode = encode_large_json(serialized)
        if len(payload) > self.settings.max_response_bytes:
            raise ValueError("趋势响应超过MCP_MAX_RESPONSE_BYTES，请缩小时间范围")
        base["payload_base64"] = payload
        return {"success": True, "data": base, "decode": decode}

    def get_feature_trend(
        self,
        device_code: str,
        point_id: str,
        start_time: str | None = None,
        end_time: str | None = None,
        days: int = 30,
        kpi_ids: list[str] | None = None,
        return_mode: str = "auto",
    ) -> dict[str, Any]:
        if return_mode not in {"auto", "inline", "binary"}:
            raise ValueError("return_mode只支持auto、inline、binary")
        start, end = default_trend_range(end_time, days)
        if start_time:
            start = parse_time(start_time) or start
        if start > end:
            raise ValueError("start_time不能晚于end_time")
        series = self.mongo.get_feature_trends(device_code, point_id, to_iso(start) or "", to_iso(end) or "", kpi_ids)
        return self._trend_response(point_id, to_iso(start) or "", to_iso(end) or "", series, return_mode)

    def get_temperature_trend(
        self,
        device_code: str,
        point_id: str,
        start_time: str | None = None,
        end_time: str | None = None,
        days: int = 30,
        kpi_id: str | None = None,
        return_mode: str = "auto",
    ) -> dict[str, Any]:
        start, end = default_trend_range(end_time, days)
        if start_time:
            start = parse_time(start_time) or start
        if start > end:
            raise ValueError("start_time不能晚于end_time")
        series = self.mongo.get_temperature_trends(device_code, point_id, to_iso(start) or "", to_iso(end) or "", kpi_id)
        return self._trend_response(point_id, to_iso(start) or "", to_iso(end) or "", series, return_mode)

    def check_data_availability(self, device_code: str, wave_point_no: str | None = None, feature_point_id: str | None = None) -> dict[str, Any]:
        return {"success": True, "data": self.mongo.availability(device_code, wave_point_no, feature_point_id), "decode": {"required": False}}


    @staticmethod
    def _data_payload_size(data: dict[str, Any]) -> int:
        payload = data.get("payload_base64")
        if isinstance(payload, str):
            return len(payload)
        series = data.get("series")
        if series is not None:
            return len(json.dumps(series, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))
        return len(json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))

    def _device_trend_day_candidates(self, requested_days: int) -> list[int]:
        candidates = [max(1, int(requested_days))]
        for token in str(self.settings.device_trend_fallback_days or "").split(","):
            token = token.strip()
            if not token:
                continue
            try:
                value = max(1, int(token))
            except ValueError:
                continue
            if value < requested_days and value not in candidates:
                candidates.append(value)
        if 1 < requested_days and 1 not in candidates:
            candidates.append(1)
        return candidates

    @staticmethod
    def _device_trend_hour_candidates(
        requested_hours: float,
        fallback_hours: float | None,
    ) -> list[float]:
        requested = float(requested_hours)
        candidates = [requested]
        if fallback_hours is not None:
            fallback = float(fallback_hours)
            if 0 < fallback < requested:
                candidates.append(fallback)
        return candidates

    @staticmethod
    def _display_hours(value: float) -> int | float:
        numeric = float(value)
        return int(numeric) if numeric.is_integer() else round(numeric, 3)

    @staticmethod
    def _window_can_fallback(exc: Exception) -> bool:
        text = str(exc).lower()
        markers = (
            "响应超过", "超过mcp_max_response_bytes", "趋势点数",
            "exceeded time limit", "execution timeout", "operation exceeded",
            "maxtimems", "query exceeded", "timed out", "timeout",
        )
        return any(marker.lower() in text for marker in markers)

    def get_device_data(
        self,
        device_code: str,
        target_time: str | None = None,
        trend_days: int = 30,
        search_window_seconds: int = 86400,
        trend_hours: float | None = None,
        fallback_trend_hours: float | None = None,
    ) -> dict[str, Any]:
        if trend_hours is None and trend_days < 1:
            raise ValueError("trend_days必须大于等于1")
        if trend_hours is not None and float(trend_hours) <= 0:
            raise ValueError("trend_hours必须大于0")

        anchor = parse_time(target_time) or now()
        errors: list[dict[str, str]] = []

        # Point discovery is intentionally independent for waveform and feature
        # collections. A transient failure on one data family must not discard the
        # other family.
        try:
            wave_points = self.mongo.list_wave_points(device_code)
        except Exception as exc:
            wave_points = []
            errors.append({"type": "wave_point_discovery", "point_no": "", "error": str(exc)})
        try:
            feature_points = self.mongo.list_feature_points(device_code)
        except Exception as exc:
            feature_points = []
            errors.append({"type": "feature_point_discovery", "point_id": "", "error": str(exc)})

        if not wave_points and not feature_points and errors:
            details = "; ".join(item.get("error", "") for item in errors if item.get("error"))
            raise RuntimeError(f"设备测点发现失败，Mongo读取不可用：{details}")

        # Waveforms are independent of trend_days. Read them exactly once and keep
        # them across any trend-window fallback. This avoids the old Conversation-side
        # 30d→7d→1d retry pattern re-reading the same large waveforms repeatedly.
        waveforms: list[dict[str, Any]] = []
        waveform_decode: dict[str, Any] | None = None
        waveform_payload_chars = 0
        for point_no in wave_points:
            try:
                result = self.get_waveform(
                    device_code,
                    point_no,
                    target_time=target_time,
                    mode="nearest" if target_time else "latest",
                    search_window_seconds=search_window_seconds,
                )
                data = result.get("data")
                if not data:
                    continue
                waveforms.append(data)
                waveform_payload_chars += len(data.get("values_base64") or "")
                waveform_decode = waveform_decode or result.get("decode")
            except Exception as exc:
                errors.append({"type": "waveform", "point_no": point_no, "error": str(exc)})

        max_bytes = max(1, int(self.settings.max_response_bytes))
        budget_ratio = min(0.98, max(0.50, float(self.settings.device_response_budget_ratio)))
        response_budget = max(1, int(max_bytes * budget_ratio))
        if waveform_payload_chars > response_budget:
            raise ValueError(
                "设备全部测点波形数据本身已超过整设备安全响应预算，"
                "缩短趋势时间范围无法解决，请提高MAX_RESPONSE_BYTES/DEVICE_RESPONSE_BUDGET_RATIO"
                "或调整波形返回策略"
            )

        feature_trends: list[dict[str, Any]] = []
        if trend_hours is not None:
            requested_hours = float(trend_hours)
            candidates = self._device_trend_hour_candidates(
                requested_hours, fallback_trend_hours
            )
        else:
            requested_hours = float(int(trend_days) * 24)
            candidates = [
                float(days * 24)
                for days in self._device_trend_day_candidates(int(trend_days))
            ]
        effective_hours = requested_hours
        fallback_events: list[dict[str, Any]] = []

        if feature_points:
            built = False
            last_error: Exception | None = None
            for index, candidate_hours in enumerate(candidates):
                trend_start = to_iso(anchor - timedelta(hours=candidate_hours)) or ""
                trend_end = to_iso(anchor) or ""
                has_next = index + 1 < len(candidates)
                try:
                    # One Mongo query for all feature points. The old implementation
                    # issued one 30-day collection query per point, so 17 points could
                    # accumulate ~17× query latency before size validation.
                    bulk = self.mongo.get_device_feature_trends_bulk(
                        device_code, feature_points, trend_start, trend_end
                    )
                    current: list[dict[str, Any]] = []
                    payload_chars = waveform_payload_chars
                    for point_id in feature_points:
                        series = bulk.get(point_id) or []
                        if not series:
                            continue
                        result = self._trend_response(
                            point_id, trend_start, trend_end, series, "auto"
                        )
                        data = result.get("data")
                        if not data:
                            continue
                        decode = result.get("decode") or {}
                        if decode.get("required"):
                            data = dict(data)
                            data["decode"] = decode
                        payload_chars += self._data_payload_size(data)
                        current.append(data)

                    if payload_chars > response_budget and has_next:
                        next_hours = candidates[index + 1]
                        fallback_events.append({
                            "from_hours": self._display_hours(candidate_hours),
                            "to_hours": self._display_hours(next_hours),
                            "reason": "projected_response_budget_exceeded",
                            "estimated_payload_bytes": payload_chars,
                            "response_budget_bytes": response_budget,
                        })
                        continue
                    if payload_chars > response_budget:
                        raise ValueError(
                            "设备全部测点数据超过整设备安全响应预算，"
                            f"即使趋势窗口缩短到{self._display_hours(candidate_hours)}小时仍无法安全返回；"
                            "请提高MAX_RESPONSE_BYTES/DEVICE_RESPONSE_BUDGET_RATIO"
                        )
                    feature_trends = current
                    effective_hours = candidate_hours
                    built = True
                    break
                except Exception as exc:
                    last_error = exc
                    if has_next and self._window_can_fallback(exc):
                        next_hours = candidates[index + 1]
                        fallback_events.append({
                            "from_hours": self._display_hours(candidate_hours),
                            "to_hours": self._display_hours(next_hours),
                            "reason": "trend_query_or_payload_limit",
                            "error": str(exc),
                        })
                        continue
                    raise

            if not built and last_error is not None:
                raise last_error

        data: dict[str, Any] = {
            "device_code": device_code,
            "anchor_time": to_iso(anchor),
            "waveforms": waveforms,
            "feature_trends": feature_trends,
            "requested_trend_hours": self._display_hours(requested_hours),
            "effective_trend_hours": self._display_hours(effective_hours),
            "requested_trend_days": requested_hours / 24,
            "effective_trend_days": effective_hours / 24,
            "waveform_mode": "nearest" if target_time else "latest",
        }
        result: dict[str, Any] = {"success": True, "data": data}
        if waveform_decode:
            result["waveform_decode"] = waveform_decode
        if errors:
            result["errors"] = errors
        if fallback_events:
            result["adaptation"] = {
                "reason": "device_trend_window_fallback",
                "requested_trend_hours": self._display_hours(requested_hours),
                "effective_trend_hours": self._display_hours(effective_hours),
                "requested_trend_days": requested_hours / 24,
                "effective_trend_days": effective_hours / 24,
                "all_device_points_preserved": True,
                "waveforms_reused_without_refetch": True,
                "events": fallback_events,
            }
        result["_audit"] = {
            "wave_points": len(wave_points),
            "feature_points": len(feature_points),
            "waveforms_returned": len(waveforms),
            "feature_trends_returned": len(feature_trends),
            "requested_trend_hours": self._display_hours(requested_hours),
            "effective_trend_hours": self._display_hours(effective_hours),
            "requested_trend_days": requested_hours / 24,
            "effective_trend_days": effective_hours / 24,
            "fallback_count": len(fallback_events),
        }
        return result

    def get_data_snapshot(
        self,
        device_code: str,
        wave_point_no: str | None = None,
        feature_point_id: str | None = None,
        target_time: str | None = None,
        kpi_ids: list[str] | None = None,
        trend_days: int = 30,
        tolerance_seconds: int = 3600,
        search_window_seconds: int = 86400,
        trend_hours: float | None = None,
    ) -> dict[str, Any]:
        requested_anchor = parse_time(target_time)
        waveform_result = None
        anchor = requested_anchor
        if wave_point_no:
            waveform_result = self.get_waveform(
                device_code,
                wave_point_no,
                target_time=target_time,
                mode="nearest" if target_time else "latest",
                search_window_seconds=search_window_seconds,
            )
            if waveform_result.get("data") and not anchor:
                anchor = parse_time(waveform_result["data"].get("resolved_time"))
        anchor = anchor or now()

        trend_result = None
        if feature_point_id:
            end = to_iso(anchor)
            window_hours = (
                float(trend_hours)
                if trend_hours is not None
                else float(max(1, trend_days) * 24)
            )
            if window_hours <= 0:
                raise ValueError("trend_hours必须大于0")
            start = to_iso(anchor - timedelta(hours=window_hours))
            trend_result = self.get_feature_trend(
                device_code,
                feature_point_id,
                start_time=start,
                end_time=end,
                kpi_ids=kpi_ids,
                return_mode="auto",
            )

        warnings: list[str] = []
        if waveform_result and waveform_result.get("data") and requested_anchor:
            offset = abs(int(waveform_result["data"].get("offset_seconds") or 0))
            if offset > tolerance_seconds:
                warnings.append(f"最近波形与目标时间相差{offset}秒，超过允许偏差{tolerance_seconds}秒")

        data: dict[str, Any] = {"anchor_time": to_iso(anchor)}
        effective_hours = (
            float(trend_hours)
            if trend_hours is not None
            else float(max(1, trend_days) * 24)
        )
        data["requested_trend_hours"] = self._display_hours(effective_hours)
        data["effective_trend_hours"] = self._display_hours(effective_hours)
        data["waveform_mode"] = "nearest" if target_time else "latest"
        if waveform_result is not None:
            data["waveform"] = {k: v for k, v in waveform_result.items() if k != "success"}
        if trend_result is not None:
            data["feature_trend"] = {k: v for k, v in trend_result.items() if k != "success"}
        result: dict[str, Any] = {"success": True, "data": data}
        if warnings:
            result["warnings"] = warnings
        return result
