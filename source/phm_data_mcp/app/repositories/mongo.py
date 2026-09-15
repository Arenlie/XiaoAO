from __future__ import annotations

from collections import defaultdict
import time
from datetime import datetime, timedelta
from typing import Any

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import AutoReconnect, NetworkTimeout, ServerSelectionTimeoutError

from app.config import Settings
from app.models import TrendSample, TrendSeries
from app.time_utils import parse_time, to_iso, to_millis, timezone


KPI_NAMES = {
    "000": "温度",
    "001": "通频速度有效值",
    "002": "低频加速度有效值",
    "003": "高频加速度有效值",
    "004": "峰值",
    "005": "冲击值",
    "006": "峭度指标",
}


def waveform_collection(device_code: str) -> str:
    return f"wave_byte_{device_code}"


def feature_collection(device_code: str) -> str:
    return f"eige_{device_code}"


def _kpi_name(kpi_id: str) -> str | None:
    suffix = str(kpi_id)[-3:]
    return KPI_NAMES.get(suffix)


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("$date", "$numberLong", "$numberInt"):
            if key in value:
                return _as_datetime(value[key])
    return parse_time(value)


def _time_for_mongo(dt: datetime, sample_value: Any) -> Any:
    """Use the same storage type as an existing Mongo time value."""
    if isinstance(sample_value, datetime):
        return dt
    if isinstance(sample_value, (int, float)):
        return int(dt.timestamp() * (1000 if float(sample_value) > 1e12 else 1))
    if isinstance(sample_value, str):
        if sample_value.replace(".", "", 1).isdigit():
            return str(int(dt.timestamp() * (1000 if float(sample_value) > 1e12 else 1)))
        if "T" in sample_value:
            return dt.isoformat(timespec="seconds")
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    return to_millis(dt)


class MongoRepository:
    """All PHM waveform and feature-trend reads live here."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = MongoClient(
            settings.mongo_uri,
            serverSelectionTimeoutMS=settings.mongo_server_selection_timeout_seconds * 1000,
            connectTimeoutMS=settings.mongo_connect_timeout_seconds * 1000,
            socketTimeoutMS=settings.mongo_socket_timeout_seconds * 1000,
            readPreference=settings.mongo_read_preference,
            retryReads=True,
            tz_aware=True,
            tzinfo=timezone(),
        )
        self.db = self.client[settings.mongo_db]

    def close(self) -> None:
        self.client.close()

    def ping(self) -> bool:
        self.client.admin.command("ping")
        return True

    def _read_with_retry(self, operation: str, fn):
        """Retry transient replica/network read failures on another healthy node.

        ``get_device_data`` performs many read operations.  A single slow/unreachable
        Mongo member must not abort the whole device diagnosis when another replica is
        healthy.  PyMongo retryReads is kept enabled, and this outer retry covers
        commands/iteration paths that still surface AutoReconnect/NetworkTimeout.
        """

        attempts = max(1, int(self.settings.mongo_read_retry_attempts))
        last: Exception | None = None
        for index in range(attempts):
            try:
                return fn()
            except (AutoReconnect, NetworkTimeout, ServerSelectionTimeoutError) as exc:
                last = exc
                if index + 1 >= attempts:
                    break
                delay = max(0.0, float(self.settings.mongo_read_retry_backoff_seconds)) * (index + 1)
                if delay:
                    time.sleep(delay)
        assert last is not None
        raise RuntimeError(f"Mongo读取失败[{operation}]，已重试{attempts}次：{last}") from last

    def _find_wave_meta(self, device_code: str, point_no: str, mode: str, target_time: datetime | None, search_window_seconds: int) -> dict[str, Any] | None:
        collection = self.db[waveform_collection(device_code)]
        projection = {"byteValues": 0, "waveValues": 0, "values": 0, "data": 0, "bytes": 0}

        # Latest is the default when no target time is provided.
        if mode == "latest" or target_time is None:
            return self._read_with_retry(
                "find_wave_meta_latest",
                lambda: collection.find_one(
                    {"pointNo": point_no},
                    projection=projection,
                    sort=[("dataTime", DESCENDING)],
                    max_time_ms=self.settings.mongo_query_max_time_ms,
                ),
            )

        sample = self._read_with_retry(
            "find_wave_meta_sample",
            lambda: collection.find_one(
                {"pointNo": point_no},
                projection={"dataTime": 1},
                sort=[("dataTime", DESCENDING)],
                max_time_ms=self.settings.mongo_query_max_time_ms,
            ),
        )
        if not sample or sample.get("dataTime") is None:
            return None

        target = _time_for_mongo(target_time, sample["dataTime"])
        start = _time_for_mongo(target_time - timedelta(seconds=search_window_seconds), sample["dataTime"])
        end = _time_for_mongo(target_time + timedelta(seconds=search_window_seconds), sample["dataTime"])

        if mode == "latest_before":
            return self._read_with_retry(
                "find_wave_meta_latest_before",
                lambda: collection.find_one(
                    {"pointNo": point_no, "dataTime": {"$lte": target}},
                    projection=projection,
                    sort=[("dataTime", DESCENDING)],
                    max_time_ms=self.settings.mongo_query_max_time_ms,
                ),
            )

        before = self._read_with_retry(
            "find_wave_meta_before",
            lambda: collection.find_one(
                {"pointNo": point_no, "dataTime": {"$gte": start, "$lte": target}},
                projection=projection,
                sort=[("dataTime", DESCENDING)],
                max_time_ms=self.settings.mongo_query_max_time_ms,
            ),
        )
        after = self._read_with_retry(
            "find_wave_meta_after",
            lambda: collection.find_one(
                {"pointNo": point_no, "dataTime": {"$gte": target, "$lte": end}},
                projection=projection,
                sort=[("dataTime", ASCENDING)],
                max_time_ms=self.settings.mongo_query_max_time_ms,
            ),
        )
        candidates = [doc for doc in (before, after) if doc]
        if not candidates:
            return None
        return min(candidates, key=lambda doc: abs((_as_datetime(doc.get("dataTime")) - target_time).total_seconds()))

    def get_waveform(self, device_code: str, point_no: str, mode: str = "latest", target_time: str | None = None, search_window_seconds: int = 86400) -> dict[str, Any] | None:
        target = parse_time(target_time)
        meta = self._find_wave_meta(device_code, point_no, mode, target, search_window_seconds)
        if not meta:
            return None
        collection = self.db[waveform_collection(device_code)]
        doc = self._read_with_retry(
            "get_waveform_payload",
            lambda: collection.find_one({"_id": meta["_id"]}),
        )
        if not doc:
            return None
        raw = None
        for key in ("byteValues", "waveValues", "values", "data", "bytes"):
            if doc.get(key) is not None:
                raw = doc[key]
                break
        sample_rate = None
        for key in ("samplingRate", "sampleRate", "sampling_rate", "sample_rate", "fs", "sampleFreq"):
            if doc.get(key) is not None:
                sample_rate = float(doc[key])
                break
        if sample_rate is None or sample_rate <= 0:
            raise ValueError("波形文档缺少有效采样率")
        resolved = _as_datetime(doc.get("dataTime"))
        return {
            "raw": raw,
            "sample_rate_hz": sample_rate,
            "resolved_time": to_iso(resolved),
            "offset_seconds": int((resolved - target).total_seconds()) if resolved and target else None,
        }

    def get_feature_trends(self, device_code: str, point_id: str, start_time: str, end_time: str, kpi_ids: list[str] | None = None) -> list[TrendSeries]:
        start = parse_time(start_time)
        end = parse_time(end_time)
        if not start or not end:
            raise ValueError("start_time和end_time不能为空")
        start_ms, end_ms = to_millis(start), to_millis(end)
        query: dict[str, Any] = {"pointId": point_id, "ft": {"$lte": end_ms}, "lt": {"$gte": start_ms}}
        if kpi_ids:
            query["kpiId"] = {"$in": list(kpi_ids)}

        collection = self.db[feature_collection(device_code)]
        docs = self._read_with_retry(
            "get_feature_trends",
            lambda: list(
                collection
                .find(query, {"pointId": 1, "kpiId": 1, "samples": 1, "unit": 1})
                .sort("ft", ASCENDING)
                .max_time_ms(self.settings.mongo_query_max_time_ms)
            ),
        )
        grouped: dict[str, list[TrendSample]] = defaultdict(list)
        units: dict[str, str] = {}
        for doc in docs:
            kpi_id = str(doc.get("kpiId") or "")
            if not kpi_id:
                continue
            if doc.get("unit"):
                units[kpi_id] = str(doc["unit"])
            for sample in doc.get("samples") or []:
                dt = _as_datetime(sample.get("ts"))
                if dt is None or dt < start or dt > end:
                    continue
                value = sample.get("value")
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                grouped[kpi_id].append(TrendSample(to_iso(dt) or "", number, sample.get("dataQua")))

        result: list[TrendSeries] = []
        for kpi_id, samples in grouped.items():
            samples.sort(key=lambda x: x.time)
            result.append(TrendSeries(kpi_id=kpi_id, name=_kpi_name(kpi_id), unit=units.get(kpi_id), samples=samples))
        result.sort(key=lambda x: x.kpi_id)
        return result

    def get_device_feature_trends_bulk(
        self,
        device_code: str,
        point_ids: list[str],
        start_time: str,
        end_time: str,
    ) -> dict[str, list[TrendSeries]]:
        """Read all requested device feature points in one Mongo query.

        Older ``get_device_data`` issued one collection query per point. A device with
        17 feature points could therefore accumulate 17 independent 45s scans before
        discovering that the final payload was too large. The collection is already
        indexed by pointId/kpiId/ft/lt, so a single ``pointId:$in`` read is both more
        predictable and dramatically cheaper in network round trips.
        """
        if not point_ids:
            return {}
        start = parse_time(start_time)
        end = parse_time(end_time)
        if not start or not end:
            raise ValueError("start_time和end_time不能为空")
        start_ms, end_ms = to_millis(start), to_millis(end)
        query: dict[str, Any] = {
            "pointId": {"$in": list(point_ids)},
            "ft": {"$lte": end_ms},
            "lt": {"$gte": start_ms},
        }
        collection = self.db[feature_collection(device_code)]
        docs = self._read_with_retry(
            "get_device_feature_trends_bulk",
            lambda: list(
                collection
                .find(query, {"pointId": 1, "kpiId": 1, "samples": 1, "unit": 1})
                .max_time_ms(self.settings.mongo_query_max_time_ms)
            ),
        )

        grouped: dict[str, dict[str, list[TrendSample]]] = defaultdict(lambda: defaultdict(list))
        units: dict[tuple[str, str], str] = {}
        for doc in docs:
            point_id = str(doc.get("pointId") or "")
            kpi_id = str(doc.get("kpiId") or "")
            if not point_id or not kpi_id:
                continue
            if doc.get("unit"):
                units[(point_id, kpi_id)] = str(doc["unit"])
            for sample in doc.get("samples") or []:
                dt = _as_datetime(sample.get("ts"))
                if dt is None or dt < start or dt > end:
                    continue
                try:
                    number = float(sample.get("value"))
                except (TypeError, ValueError):
                    continue
                grouped[point_id][kpi_id].append(
                    TrendSample(to_iso(dt) or "", number, sample.get("dataQua"))
                )

        result: dict[str, list[TrendSeries]] = {}
        for point_id in point_ids:
            series: list[TrendSeries] = []
            for kpi_id, samples in grouped.get(point_id, {}).items():
                samples.sort(key=lambda item: item.time)
                series.append(
                    TrendSeries(
                        kpi_id=kpi_id,
                        name=_kpi_name(kpi_id),
                        unit=units.get((point_id, kpi_id)),
                        samples=samples,
                    )
                )
            series.sort(key=lambda item: item.kpi_id)
            if series:
                result[point_id] = series
        return result

    def get_temperature_trends(self, device_code: str, point_id: str, start_time: str, end_time: str, kpi_id: str | None = None) -> list[TrendSeries]:
        selected = kpi_id or f"{point_id}000"
        return self.get_feature_trends(device_code, point_id, start_time, end_time, [selected])


    def list_wave_points(self, device_code: str) -> list[str]:
        collection = self.db[waveform_collection(device_code)]
        values = self._read_with_retry(
            "list_wave_points",
            lambda: collection.distinct(
                "pointNo",
                {"pointNo": {"$exists": True, "$nin": [None, ""]}},
            ),
        )
        return sorted({str(value) for value in values if value is not None and str(value)})

    def list_feature_points(self, device_code: str) -> list[str]:
        collection = self.db[feature_collection(device_code)]
        values = self._read_with_retry(
            "list_feature_points",
            lambda: collection.distinct(
                "pointId",
                {"pointId": {"$exists": True, "$nin": [None, ""]}},
            ),
        )
        return sorted({str(value) for value in values if value is not None and str(value)})

    def availability(self, device_code: str, wave_point_no: str | None, feature_point_id: str | None) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if wave_point_no:
            doc = self.db[waveform_collection(device_code)].find_one(
                {"pointNo": wave_point_no}, {"_id": 1, "dataTime": 1}, max_time_ms=self.settings.mongo_query_max_time_ms
            )
            data["waveform"] = bool(doc)
            if doc:
                data["latest_waveform_time"] = to_iso(_as_datetime(doc.get("dataTime")))
        if feature_point_id:
            docs = list(
                self.db[feature_collection(device_code)]
                .find({"pointId": feature_point_id}, {"kpiId": 1})
                .limit(200)
                .max_time_ms(self.settings.mongo_query_max_time_ms)
            )
            kpis = sorted({str(doc.get("kpiId")) for doc in docs if doc.get("kpiId")})
            data["feature_trend"] = bool(kpis)
            data["kpi_ids"] = kpis
        return data
