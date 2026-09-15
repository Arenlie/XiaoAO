from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.config import Settings
from app.domain.phm_point_codes import enrich_phm_point_entity
from app.integrations.phm_data_mcp import PhmDataMcpClient
from app.integrations.phm_diagnosis_mcp import PhmDiagnosisMcpClient
from app.integrations.phm_feature_mcp import PhmFeatureMcpClient
from app.orchestration.entity_lifecycle import current_turn_entity
from app.orchestration.runtime import current_graph_runtime
from app.tools.contracts import (
    ToolCallRequest,
    ToolCallResult,
    ToolDescriptor,
    ToolProviderType,
    ToolResultStatus,
)
from app.tools.phm_asset_mcp import (
    PHM_ASSET_QUERY_POINTS_TOOL_ID,
    PHM_ASSET_QUERY_SCOPE_COLLECTION_TOOL_ID,
)
from app.tools.phm_data_context import build_phm_data_arguments
from app.tools.phm_data_mcp import (
    PHM_GET_DATA_SNAPSHOT_TOOL_ID,
    PHM_QUERY_ALARM_RECORDS_TOOL_ID,
    PHM_QUERY_HEALTH_SCORE_TOOL_ID,
)
from app.tools.phm_diagnosis_context import (
    build_phm_diagnosis_arguments,
    compact_diagnosis_result,
)
from app.tools.phm_diagnosis_mcp import PHM_DIAGNOSIS_POINT_TOOL_ID
from app.tools.phm_feature_context import (
    build_phm_feature_arguments,
    extract_supported_rpm,
)
from app.tools.phm_feature_mcp import PHM_FEATURE_EXTRACT_RPM_TOOL_ID


PHM_WORKFLOW_HEALTH_DIMENSIONS_TOOL_ID = "workflow.phm.health_dimensions"
PHM_WORKFLOW_HEALTH_ALARMS_TOOL_ID = "workflow.phm.health_dimension_alarms"
PHM_WORKFLOW_HEALTH_COLLECTION_BATCH_TOOL_ID = "workflow.phm.health_collection_batch"
PHM_WORKFLOW_POINT_DATA_BATCH_TOOL_ID = "workflow.phm.point_data_batch"
PHM_WORKFLOW_POINT_RPM_BATCH_TOOL_ID = "workflow.phm.point_rpm_batch"
PHM_WORKFLOW_POINT_DIAGNOSIS_BATCH_TOOL_ID = "workflow.phm.point_diagnosis_batch"
PHM_WORKFLOW_RAG_STRUCTURE_STATUS_TOOL_ID = "workflow.phm.rag_structure_status"

from app.query_contract import QUERY_TOOL_ID, RESULT_TOOL_ID

PHM_WORKFLOW_TOOL_IDS = {
    QUERY_TOOL_ID, RESULT_TOOL_ID,
    PHM_WORKFLOW_HEALTH_DIMENSIONS_TOOL_ID,
    PHM_WORKFLOW_HEALTH_ALARMS_TOOL_ID,
    PHM_WORKFLOW_HEALTH_COLLECTION_BATCH_TOOL_ID,
    PHM_WORKFLOW_POINT_DATA_BATCH_TOOL_ID,
    PHM_WORKFLOW_POINT_RPM_BATCH_TOOL_ID,
    PHM_WORKFLOW_POINT_DIAGNOSIS_BATCH_TOOL_ID,
    PHM_WORKFLOW_RAG_STRUCTURE_STATUS_TOOL_ID,
}

_HEALTH_DIMENSIONS: tuple[tuple[str, str, str], ...] = (
    ("thresholdScore", "阈值模型", "threshold"),
    ("trendScore", "趋势模型", "trend"),
    ("aiScore", "AI模型", "ai"),
    ("mechanismScore", "机理模型", "diagnosis"),
)


def workflow_tool_required_entity_level(tool_id: str, requested: str = "none") -> str:
    if tool_id in {QUERY_TOOL_ID, RESULT_TOOL_ID}:
        return "none"
    explicit = str(requested or "none").strip().lower()
    if explicit in {"area", "equipment", "point"}:
        return explicit
    if tool_id == PHM_WORKFLOW_HEALTH_COLLECTION_BATCH_TOOL_ID:
        return "area"
    if tool_id in {
        PHM_WORKFLOW_HEALTH_DIMENSIONS_TOOL_ID,
        PHM_WORKFLOW_HEALTH_ALARMS_TOOL_ID,
        PHM_WORKFLOW_POINT_DATA_BATCH_TOOL_ID,
        PHM_WORKFLOW_POINT_RPM_BATCH_TOOL_ID,
        PHM_WORKFLOW_POINT_DIAGNOSIS_BATCH_TOOL_ID,
    }:
        return "equipment"
    return "none"


def build_workflow_tool_arguments(
    *,
    tool_id: str,
    state: Mapping[str, Any],
    call_arguments: Mapping[str, Any],
) -> dict[str, Any]:
    arguments = {
        key: value
        for key, value in dict(call_arguments).items()
        if key not in {"objective"} and value not in (None, "")
    }
    decision = (
        dict(state.get("entity_resolution") or {})
        if isinstance(state.get("entity_resolution"), Mapping)
        else {}
    )
    entity = current_turn_entity(state)
    arguments["_workflow_context"] = {
        "query": str(state.get("query") or ""),
        "query_plan": (state.get("business_intent") or {}).get("query_plan") or {},
        "result_followup": (state.get("business_intent") or {}).get("result_followup") or {},
        "answer_results": (state.get("memory_context") or {}).get("answer_results") or [],
        "task_id": str(state.get("task_id") or ""),
        "entity": entity,
        "query_scope": dict(state.get("query_scope") or {})
        if isinstance(state.get("query_scope"), Mapping)
        else {},
        "entity_constraints": dict(state.get("entity_constraints") or {})
        if isinstance(state.get("entity_constraints"), Mapping)
        else {},
        "entity_resolution_action": decision.get("action"),
    }
    return arguments


def _state_from_context(
    context: Mapping[str, Any], entity: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "query": str(context.get("query") or ""),
        "task_id": str(context.get("task_id") or ""),
        "resolved_entity": dict(entity or context.get("entity") or {}),
        "query_scope": dict(context.get("query_scope") or {}),
        "entity_constraints": dict(context.get("entity_constraints") or {}),
        "entity_resolution": {"action": "reuse"},
    }


def _payload_data(payload: Mapping[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, Mapping):
        return dict(data)
    return dict(payload)


def _error_detail(exc: BaseException) -> dict[str, str]:
    return {
        "code": str(getattr(exc, "code", type(exc).__name__)),
        "message": str(getattr(exc, "message", None) or str(exc) or type(exc).__name__),
    }


def _is_feature_queue_pressure(error: Mapping[str, Any]) -> bool:
    message = str(error.get("message") or "").lower()
    return "analysis queue wait exceeded" in message or "queue wait exceeded" in message


def _public_point_identity(value: Any) -> dict[str, Any]:
    """Keep the point facts needed by the final model without verbose duplicate metadata."""

    point = dict(value) if isinstance(value, Mapping) else {}
    metadata = point.get("metadata") if isinstance(point.get("metadata"), Mapping) else {}

    def first(*keys: str) -> Any:
        for key in keys:
            candidate = point.get(key)
            if candidate not in (None, "", [], {}):
                return candidate
            candidate = metadata.get(key)
            if candidate not in (None, "", [], {}):
                return candidate
        return None

    fields = {
        "point_no": first("point_no", "wave_point_no", "source_point_code"),
        "point_name": first("point_name"),
        "equip_no": first("equip_no"),
        "equip_name": first("equip_name", "equipment_name"),
        "component_name": first("component_name", "station_name"),
        "position_name": first("position_name", "install_position"),
        "direction_name": first("direction_name", "direction"),
        "measurement_name": first("measurement_name", "param_name"),
    }
    return {key: item for key, item in fields.items() if item not in (None, "", [], {})}


def _compact_spectrum_peaks(value: Any, *, limit: int = 5) -> list[dict[str, Any]]:
    source = value if isinstance(value, Mapping) else {}
    rows: list[dict[str, Any]] = []
    for raw in source.get("top_peaks") or []:
        if not isinstance(raw, Mapping):
            continue
        row = {
            key: raw.get(key)
            for key in ("frequency_hz", "amplitude", "order", "quefrency_s")
            if raw.get(key) not in (None, "", [], {})
        }
        if row:
            rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def _compact_trend_findings(value: Any, *, limit: int = 12) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    groups = value if isinstance(value, list) else []
    for group in groups:
        if not isinstance(group, Mapping):
            continue
        for raw in group.get("series") or []:
            if not isinstance(raw, Mapping):
                continue
            row = {
                key: raw.get(key)
                for key in (
                    "name",
                    "kpi_id",
                    "trend_label",
                    "start_level",
                    "end_level",
                    "change_percent",
                    "max_step_z",
                    "outlier_ratio",
                )
                if raw.get(key) not in (None, "", [], {})
            }
            if row:
                findings.append(row)
            if len(findings) >= limit:
                return findings
    return findings


def _compact_diagnosis_for_synthesis(value: Any) -> dict[str, Any]:
    """Project one Diagnosis MCP result to a bounded, model-visible evidence record."""

    source = dict(value) if isinstance(value, Mapping) else {}
    analysis = source.get("analysis") if isinstance(source.get("analysis"), Mapping) else {}
    waveform = analysis.get("waveform") if isinstance(analysis.get("waveform"), Mapping) else {}
    time_domain = (
        waveform.get("time_domain")
        if isinstance(waveform.get("time_domain"), Mapping)
        else {}
    )
    scalar_metrics = {
        key: time_domain.get(key)
        for key in (
            "rms",
            "abs_peak",
            "peak_to_peak",
            "kurtosis",
            "skewness",
            "crest_factor",
            "impulse_factor",
            "clearance_factor",
        )
        if time_domain.get(key) not in (None, "", [], {})
    }
    waveform_evidence = {
        "time_domain": scalar_metrics,
        "frequency_peaks": _compact_spectrum_peaks(waveform.get("frequency_spectrum")),
        "envelope_peaks": _compact_spectrum_peaks(waveform.get("envelope_spectrum")),
    }
    waveform_evidence = {
        key: item for key, item in waveform_evidence.items() if item not in (None, "", [], {})
    }
    result = {
        "success": source.get("success"),
        "point_no": source.get("point_no"),
        "status": source.get("status"),
        "summary": source.get("summary"),
        "fault_hypotheses": compact_diagnosis_result(source.get("fault_hypotheses") or []),
        "recommendations": compact_diagnosis_result(source.get("recommendations") or []),
        "limitations": compact_diagnosis_result(source.get("limitations") or []),
        "waveform_evidence": waveform_evidence,
        "trend_findings": _compact_trend_findings(analysis.get("feature_trends")),
    }
    return {key: item for key, item in result.items() if item not in (None, "", [], {})}


def _data_snapshot_has_waveform(payload: Mapping[str, Any]) -> bool:
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
    wrapper = data.get("waveform") if isinstance(data.get("waveform"), Mapping) else {}
    waveform = wrapper.get("data") if isinstance(wrapper.get("data"), Mapping) else wrapper
    if not isinstance(waveform, Mapping):
        return False
    encoded = waveform.get("values_base64") or waveform.get("float32_base64")
    try:
        sample_rate = float(waveform.get("sample_rate_hz") or waveform.get("fs_hz") or 0)
    except (TypeError, ValueError):
        sample_rate = 0.0
    return isinstance(encoded, str) and bool(encoded) and sample_rate > 0


def _device_bundle_snapshot(
    bundle: Mapping[str, Any],
    *,
    wave_point_no: str,
    feature_point_id: str,
) -> dict[str, Any] | None:
    """Project one Data MCP device bundle back to the point-snapshot contract."""

    data = bundle.get("data") if isinstance(bundle.get("data"), Mapping) else {}
    waveforms = [item for item in data.get("waveforms") or [] if isinstance(item, Mapping)]
    trends = [item for item in data.get("feature_trends") or [] if isinstance(item, Mapping)]
    waveform = next(
        (
            dict(item)
            for item in waveforms
            if str(item.get("point_no") or "").upper() == wave_point_no.upper()
        ),
        None,
    )
    if waveform is None:
        return None

    trend = next(
        (
            dict(item)
            for item in trends
            if str(item.get("point_id") or "").upper() == feature_point_id.upper()
        ),
        None,
    )
    waveform_wrapper: dict[str, Any] = {"data": waveform}
    waveform_decode = bundle.get("waveform_decode")
    if isinstance(waveform_decode, Mapping):
        waveform_wrapper["decode"] = dict(waveform_decode)

    snapshot_data: dict[str, Any] = {
        "anchor_time": data.get("anchor_time"),
        "waveform": waveform_wrapper,
    }
    if trend is not None:
        trend_decode = trend.pop("decode", None)
        trend_wrapper: dict[str, Any] = {"data": trend}
        if isinstance(trend_decode, Mapping):
            trend_wrapper["decode"] = dict(trend_decode)
        snapshot_data["feature_trend"] = trend_wrapper
    return {
        "success": True,
        "data": snapshot_data,
        "source": "get_device_data",
        "effective_trend_days": data.get("effective_trend_days"),
        "requested_trend_hours": data.get("requested_trend_hours"),
        "effective_trend_hours": data.get("effective_trend_hours"),
    }


def _compact_point_failure(item: Mapping[str, Any]) -> dict[str, Any]:
    point = item.get("point") if isinstance(item.get("point"), Mapping) else {}
    error = item.get("error") if isinstance(item.get("error"), Mapping) else {}
    return {
        "point_no": point.get("point_no") or point.get("wave_point_no"),
        "point_name": point.get("point_name"),
        "error_code": error.get("code") or "POINT_DATA_FAILED",
        "error_message": error.get("message") or "测点数据读取失败",
    }


def _can_retry_with_shorter_trend(error: Mapping[str, Any]) -> bool:
    code = str(error.get("code") or "").upper()
    if code in {"PHM_DATA_MCP_UNAVAILABLE", "PHM_DATA_MCP_RESPONSE_STREAM_FAILED"}:
        return False
    if code == "PHM_DATA_MCP_TIMEOUT":
        return True
    message = str(error.get("message") or "").lower()
    return any(
        marker in message
        for marker in (
            "mcp_max_response_bytes",
            "响应超过",
            "响应预算",
            "趋势点数",
            "query exceeded",
            "exceeded time limit",
            "operation exceeded",
            "maxtimems",
        )
    )


def _health_target_time(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric > 10_000_000_000:
            numeric /= 1000.0
        try:
            return datetime.fromtimestamp(numeric, ZoneInfo("Asia/Shanghai")).isoformat(
                timespec="seconds"
            )
        except (OverflowError, OSError, ValueError):
            return None
    return str(value).strip() or None


class PhmWorkflowToolHandlers:
    """Deterministic orchestration used only by the three approved PHM workflows."""

    def __init__(
        self,
        *,
        data_client: PhmDataMcpClient,
        feature_client: PhmFeatureMcpClient,
        diagnosis_client: PhmDiagnosisMcpClient,
        max_parallel: int,
        feature_max_parallel: int = 4,
        feature_queue_retry_attempts: int = 2,
        feature_queue_retry_delay_seconds: float = 4.0,
    ) -> None:
        self.data_client = data_client
        self.feature_client = feature_client
        self.diagnosis_client = diagnosis_client
        self.max_parallel = max(1, int(max_parallel))
        self.feature_max_parallel = max(1, int(feature_max_parallel))
        self.feature_queue_retry_attempts = max(0, int(feature_queue_retry_attempts))
        self.feature_queue_retry_delay_seconds = max(
            0.0, float(feature_queue_retry_delay_seconds)
        )

    async def _bounded(
        self,
        items: list[Any],
        worker: Callable[[Any], Awaitable[dict[str, Any]]],
        *,
        max_parallel: int | None = None,
    ) -> list[dict[str, Any]]:
        semaphore = asyncio.Semaphore(max(1, int(max_parallel or self.max_parallel)))

        async def run(item: Any) -> dict[str, Any]:
            async with semaphore:
                return await worker(item)

        return list(await asyncio.gather(*(run(item) for item in items)))

    @staticmethod
    def _context(request: ToolCallRequest) -> dict[str, Any]:
        value = request.arguments.get("_workflow_context")
        return dict(value) if isinstance(value, Mapping) else {}

    @staticmethod
    def _collection_entity_public(entity: Mapping[str, Any]) -> dict[str, Any]:
        source = dict(entity)
        metadata = source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {}

        def first(*keys: str) -> Any:
            for key in keys:
                value = source.get(key)
                if value not in (None, "", [], {}):
                    return value
                value = metadata.get(key)
                if value not in (None, "", [], {}):
                    return value
            return None

        entity_type = str(first("entity_type", "node_type") or "").strip().lower()
        if entity_type in {"area", "space"}:
            entity_type = "space"
        elif entity_type in {"device", "equipment"}:
            entity_type = "equipment"
        return {
            key: value
            for key, value in {
                "entity_type": entity_type,
                "space_id": first("space_id", "spaceId"),
                "space_name": first("space_name", "leaf_space_name", "area_name"),
                "space_type": first("space_type", "leaf_space_type"),
                "space_path": first("space_path", "path"),
                "equip_no": first("equip_no", "device_code", "equipment_no", "equipNo"),
                "equip_name": first("equip_name", "equipment_name", "display_name"),
                "equipment_type": first("equipment_type", "equip_type"),
                "point_no": first("point_no", "pointNo", "wave_point_no"),
                "point_name": first("point_name", "pointName", "display_name"),
            }.items()
            if value not in (None, "", [], {})
        }

    @staticmethod
    def _health_payload_for_synthesis(payload: Mapping[str, Any]) -> Any:
        data = payload.get("data")
        if isinstance(data, list):
            return [dict(item) if isinstance(item, Mapping) else item for item in data]
        if isinstance(data, Mapping):
            return dict(data)
        return _payload_data(payload)

    async def health_collection_batch(
        self, request: ToolCallRequest, data_access_token: str | None
    ) -> ToolCallResult:
        """Read health for every entity returned by Asset scope collection.

        Language interpretation is intentionally absent here.  Asset MCP already
        returned real descendant entities, and this executor only fans out the
        authoritative Data MCP health query with bounded concurrency.  Lowest/highest
        ranking is left to the final Supervisor model over the returned facts.
        """

        del data_access_token
        transient = current_graph_runtime().transient_tool_payloads
        raw_collection = transient.get(f"latest:{PHM_ASSET_QUERY_SCOPE_COLLECTION_TOOL_ID}")
        collection_payload = (
            dict(raw_collection) if isinstance(raw_collection, Mapping) else {}
        )
        target_level = str(collection_payload.get("target_entity_level") or "").strip().lower()
        entities = [
            dict(item)
            for item in collection_payload.get("collection") or []
            if isinstance(item, Mapping)
        ]
        if target_level not in {"space", "equipment"}:
            return ToolCallResult(
                tool_id=request.tool_id,
                status=ToolResultStatus.FAILED,
                error_code="HEALTH_COLLECTION_TARGET_UNSUPPORTED",
                error_message=(
                    "健康度批量查询只支持空间或设备集合；"
                    f"当前集合层级为 {target_level or 'unknown'}。"
                ),
            )
        if not entities:
            return ToolCallResult(
                tool_id=request.tool_id,
                status=ToolResultStatus.SUCCESS,
                structured_content={
                    "success": True,
                    "source": "phm_data_mcp",
                    "target_entity_level": target_level,
                    "requested_count": 0,
                    "success_count": 0,
                    "failure_count": 0,
                    "items": [],
                    "failures": [],
                    "note": "Asset MCP 在该区域下未返回符合目标层级的实体。",
                },
            )

        async def worker(entity: dict[str, Any]) -> dict[str, Any]:
            started_ns = time.perf_counter_ns()
            public_entity = self._collection_entity_public(entity)
            if target_level == "space":
                scope_id = str(public_entity.get("space_id") or "").strip()
                scope_type = "space"
            else:
                scope_id = str(public_entity.get("equip_no") or "").strip()
                scope_type = "device"
            if not scope_id:
                return {
                    "entity": public_entity,
                    "success": False,
                    "error": {
                        "code": "HEALTH_COLLECTION_IDENTITY_MISSING",
                        "message": "集合实体缺少 Data MCP 所需的确定性编码",
                    },
                    "duration_ms": (time.perf_counter_ns() - started_ns) / 1_000_000.0,
                }
            try:
                payload = await self.data_client.call_tool(
                    "query_health_score",
                    {"scope_type": scope_type, "scope_id": scope_id},
                )
            except Exception as exc:
                return {
                    "entity": public_entity,
                    "success": False,
                    "error": _error_detail(exc),
                    "duration_ms": (time.perf_counter_ns() - started_ns) / 1_000_000.0,
                }
            return {
                "entity": public_entity,
                "success": bool(payload.get("success", True)),
                "health": self._health_payload_for_synthesis(payload),
                "duration_ms": (time.perf_counter_ns() - started_ns) / 1_000_000.0,
            }

        rows = await self._bounded(entities, worker)
        items = [row for row in rows if row.get("success")]
        failures = [row for row in rows if not row.get("success")]
        result = {
            "success": True,
            "source": "phm_data_mcp",
            "root": collection_payload.get("root") or {},
            "target_entity_level": target_level,
            "target_space_type": collection_payload.get("target_space_type"),
            "requested_count": len(entities),
            "success_count": len(items),
            "failure_count": len(failures),
            "items": items,
            "failures": failures,
            "collection_truncated": bool(collection_payload.get("truncated")),
            "parallel_limit": self.max_parallel,
            "ranking_computed_in_code": False,
        }
        return ToolCallResult(
            tool_id=request.tool_id,
            status=ToolResultStatus.SUCCESS,
            structured_content=result,
        )

    async def health_dimensions(
        self, request: ToolCallRequest, data_access_token: str | None
    ) -> ToolCallResult:
        del data_access_token
        context = self._context(request)
        state = _state_from_context(context)
        arguments, missing = build_phm_data_arguments(
            tool_id=PHM_QUERY_HEALTH_SCORE_TOOL_ID,
            state=state,
            call_arguments={"scope_type": "device"},
        )
        if missing:
            return ToolCallResult(
                tool_id=request.tool_id,
                status=ToolResultStatus.FAILED,
                error_code="HEALTH_EXPLANATION_ENTITY_MISSING",
                error_message="健康度解释缺少设备编码：" + "、".join(missing),
            )
        try:
            payload = await self.data_client.call_tool("query_health_score", arguments)
        except Exception as exc:
            detail = _error_detail(exc)
            return ToolCallResult(
                tool_id=request.tool_id,
                status=ToolResultStatus.FAILED,
                error_code=detail["code"],
                error_message="读取健康度四类分项失败：" + detail["message"],
            )
        data = payload.get("data")
        if isinstance(data, list):
            record = dict(data[0]) if data and isinstance(data[0], Mapping) else {}
        else:
            record = _payload_data(payload)

        missing_dimensions = [
            key for key, _, _ in _HEALTH_DIMENSIONS if record.get(key) in (None, "")
        ]
        if missing_dimensions:
            return ToolCallResult(
                tool_id=request.tool_id,
                status=ToolResultStatus.FAILED,
                error_code="HEALTH_DIMENSIONS_INCOMPLETE",
                error_message=("健康度结果缺少四类分项字段：" + "、".join(missing_dimensions)),
            )

        deficits: list[dict[str, Any]] = []
        for key, label, alarm_type in _HEALTH_DIMENSIONS:
            value = record.get(key)
            try:
                score = float(value)
            except (TypeError, ValueError):
                continue
            if score < 100.0:
                deficits.append(
                    {
                        "score_key": key,
                        "dimension": label,
                        "score": value,
                        "gap_to_full": 100.0 - score,
                        "alarm_type": alarm_type,
                    }
                )
        result = {
            "success": True,
            "source": "phm_data_mcp",
            "health_record": record,
            "deficits": deficits,
            "deficit_count": len(deficits),
        }
        return ToolCallResult(
            tool_id=request.tool_id,
            status=ToolResultStatus.SUCCESS,
            structured_content=result,
        )

    async def health_dimension_alarms(
        self, request: ToolCallRequest, data_access_token: str | None
    ) -> ToolCallResult:
        del data_access_token
        transient = current_graph_runtime().transient_tool_payloads
        dimensions = transient.get(f"latest:{PHM_WORKFLOW_HEALTH_DIMENSIONS_TOOL_ID}")
        dimensions = dict(dimensions) if isinstance(dimensions, Mapping) else {}
        deficits = [
            dict(item) for item in dimensions.get("deficits") or [] if isinstance(item, Mapping)
        ]
        alarm_types = list(
            dict.fromkeys(
                str(item.get("alarm_type")) for item in deficits if item.get("alarm_type")
            )
        )
        if not alarm_types:
            return ToolCallResult(
                tool_id=request.tool_id,
                status=ToolResultStatus.SUCCESS,
                structured_content={
                    "success": True,
                    "deficits": deficits,
                    "alarm_types": [],
                    "alarm_result": {"success": True, "data": []},
                    "note": "四类健康度分项均为满分，未发起无依据的报警类型查询。",
                },
            )
        context = self._context(request)
        state = _state_from_context(context)
        health_record = (
            dimensions.get("health_record")
            if isinstance(dimensions.get("health_record"), Mapping)
            else {}
        )
        health_time = _health_target_time(health_record.get("ts"))
        alarm_arguments: dict[str, Any] = {
            "alarm_types": alarm_types,
            "time_mode": "nearest" if health_time not in (None, "") else "default",
            "metric": "detail",
            "limit": 50,
        }
        if health_time not in (None, ""):
            alarm_arguments["target_time"] = health_time
        arguments, missing = build_phm_data_arguments(
            tool_id=PHM_QUERY_ALARM_RECORDS_TOOL_ID,
            state=state,
            call_arguments=alarm_arguments,
        )
        if missing:
            return ToolCallResult(
                tool_id=request.tool_id,
                status=ToolResultStatus.FAILED,
                error_code="HEALTH_ALARM_SCOPE_MISSING",
                error_message="扣分项报警查询缺少范围：" + "、".join(missing),
            )
        try:
            alarms = await self.data_client.call_tool("query_alarm_records", arguments)
        except Exception as exc:
            detail = _error_detail(exc)
            return ToolCallResult(
                tool_id=request.tool_id,
                status=ToolResultStatus.FAILED,
                error_code=detail["code"],
                error_message="扣分项对应报警查询失败：" + detail["message"],
            )
        return ToolCallResult(
            tool_id=request.tool_id,
            status=ToolResultStatus.SUCCESS,
            structured_content={
                "success": True,
                "deficits": deficits,
                "alarm_types": alarm_types,
                "alarm_query": arguments,
                "alarm_result": alarms,
            },
        )

    async def point_data_batch(
        self, request: ToolCallRequest, data_access_token: str | None
    ) -> ToolCallResult:
        del data_access_token
        transient = current_graph_runtime().transient_tool_payloads
        catalog = transient.get(f"latest:{PHM_ASSET_QUERY_POINTS_TOOL_ID}")
        catalog = dict(catalog) if isinstance(catalog, Mapping) else {}
        points = [dict(item) for item in catalog.get("points") or [] if isinstance(item, Mapping)]
        context = self._context(request)
        base_entity = dict(context.get("entity") or {})
        equipment = (
            catalog.get("equipment") if isinstance(catalog.get("equipment"), Mapping) else {}
        )
        equip_no = str(
            equipment.get("equip_no")
            or base_entity.get("equip_no")
            or base_entity.get("device_code")
            or ""
        )
        if not points:
            return ToolCallResult(
                tool_id=request.tool_id,
                status=ToolResultStatus.FAILED,
                error_code="DIAGNOSIS_POINT_CATALOG_EMPTY",
                error_message="Asset MCP 未返回可用于设备诊断的振动加速度测点。",
            )

        try:
            requested_trend_hours = float(request.arguments.get("trend_hours") or 24.0)
        except (TypeError, ValueError):
            requested_trend_hours = 24.0
        if requested_trend_hours <= 0:
            requested_trend_hours = 24.0
        fallback_raw = request.arguments.get("fallback_trend_hours")
        if fallback_raw in (None, "") and "trend_hours" not in request.arguments:
            fallback_raw = 12.0
        try:
            fallback_trend_hours = (
                float(fallback_raw) if fallback_raw not in (None, "") else None
            )
        except (TypeError, ValueError):
            fallback_trend_hours = None
        if (
            fallback_trend_hours is not None
            and (fallback_trend_hours <= 0 or fallback_trend_hours >= requested_trend_hours)
        ):
            fallback_trend_hours = None

        entities: list[dict[str, Any]] = []
        for point in points:
            metadata = point.get("metadata") if isinstance(point.get("metadata"), Mapping) else {}
            entity = enrich_phm_point_entity(
                {**dict(metadata), **point, "equip_no": point.get("equip_no") or equip_no}
            )
            entities.append(entity)

        # Data MCP performs one latest-waveform discovery and one bulk trend query.  For
        # the default device diagnosis the requested trend window is 24h and the service
        # may retry 12h on timeout/payload pressure without re-reading the waveforms.
        # Project that bundle back to point snapshots so Feature/Diagnosis stay per-point.
        bundle: dict[str, Any] = {}
        bundle_error: dict[str, str] | None = None
        try:
            bundle_arguments: dict[str, Any] = {
                "device_code": equip_no,
                "trend_hours": requested_trend_hours,
                "search_window_seconds": 86400,
            }
            if fallback_trend_hours is not None:
                bundle_arguments["fallback_trend_hours"] = fallback_trend_hours
            bundle = await self.data_client.call_tool(
                "get_device_data",
                bundle_arguments,
            )
        except Exception as exc:
            bundle_error = _error_detail(exc)

        rows: list[dict[str, Any]] = []
        missing_entities: list[dict[str, Any]] = []
        for entity in entities:
            point_state = _state_from_context(context, entity)
            ids_arguments, missing = build_phm_data_arguments(
                tool_id=PHM_GET_DATA_SNAPSHOT_TOOL_ID,
                state=point_state,
                call_arguments={"trend_hours": requested_trend_hours},
            )
            if missing:
                rows.append(
                    {
                        "ok": False,
                        "point": entity,
                        "error": {
                            "code": "POINT_DATA_ARGUMENTS_MISSING",
                            "message": "、".join(missing),
                        },
                    }
                )
                continue
            snapshot = _device_bundle_snapshot(
                bundle,
                wave_point_no=str(ids_arguments.get("wave_point_no") or ""),
                feature_point_id=str(ids_arguments.get("feature_point_id") or ""),
            )
            if snapshot is not None:
                rows.append(
                    {
                        "ok": True,
                        "point": entity,
                        "arguments": ids_arguments,
                        "snapshot": snapshot,
                        "read_mode": "device_bundle",
                    }
                )
            else:
                missing_entities.append(entity)

        async def load(point: dict[str, Any]) -> dict[str, Any]:
            state = _state_from_context(context, point)
            last_error: dict[str, str] | None = None
            last_arguments: dict[str, Any] = {}
            windows = [requested_trend_hours]
            if fallback_trend_hours is not None:
                windows.append(fallback_trend_hours)
            for index, trend_hours in enumerate(windows):
                arguments, missing = build_phm_data_arguments(
                    tool_id=PHM_GET_DATA_SNAPSHOT_TOOL_ID,
                    state=state,
                    call_arguments={"trend_hours": trend_hours},
                )
                last_arguments = arguments
                if missing:
                    return {
                        "ok": False,
                        "point": point,
                        "error": {
                            "code": "POINT_DATA_ARGUMENTS_MISSING",
                            "message": "、".join(missing),
                        },
                    }
                try:
                    snapshot = await self.data_client.call_tool("get_data_snapshot", arguments)
                except Exception as exc:
                    last_error = _error_detail(exc)
                    if index + 1 < len(windows) and _can_retry_with_shorter_trend(last_error):
                        continue
                    break
                if _data_snapshot_has_waveform(snapshot):
                    return {
                        "ok": True,
                        "point": point,
                        "arguments": arguments,
                        "snapshot": snapshot,
                        "read_mode": "point_fallback",
                    }
                last_error = {
                    "code": "POINT_WAVEFORM_NOT_FOUND",
                    "message": "Data MCP 未返回该测点的可用波形。",
                }
                break
            return {
                "ok": False,
                "point": point,
                "arguments": last_arguments,
                "error": last_error or {"code": "POINT_DATA_FAILED", "message": "测点数据读取失败"},
            }

        if missing_entities:
            rows.extend(await self._bounded(missing_entities, load))
        successes = [item for item in rows if item.get("ok")]
        failures = [item for item in rows if not item.get("ok")]
        if not successes:
            compact_failures = [_compact_point_failure(item) for item in failures]
            failure_codes = {str(item.get("error_code") or "") for item in compact_failures}
            retryable_code = next(
                (
                    code
                    for code in (
                        "PHM_DATA_MCP_TIMEOUT",
                        "PHM_DATA_MCP_UNAVAILABLE",
                        "PHM_DATA_MCP_RESPONSE_STREAM_FAILED",
                    )
                    if code in failure_codes
                ),
                None,
            )
            return ToolCallResult(
                tool_id=request.tool_id,
                status=ToolResultStatus.FAILED,
                error_code=retryable_code or "DIAGNOSIS_ALL_POINT_DATA_FAILED",
                error_message=(
                    f"设备的 {len(points)} 个测点均未取得可诊断数据；"
                    f"设备批量读取错误={bundle_error or '无'}；逐点失败={compact_failures}"
                ),
            )
        bundle_data = _payload_data(bundle)
        windows_used = {
            float(item.get("arguments", {}).get("trend_hours"))
            for item in successes
            if isinstance(item.get("arguments"), Mapping)
            and item.get("arguments", {}).get("trend_hours") not in (None, "")
        }
        bundle_effective = bundle_data.get("effective_trend_hours")
        if bundle_effective not in (None, ""):
            windows_used.add(float(bundle_effective))
        effective_trend_hours = (
            min(windows_used) if windows_used else requested_trend_hours
        )
        return ToolCallResult(
            tool_id=request.tool_id,
            status=ToolResultStatus.SUCCESS,
            structured_content={
                "success": True,
                "equipment": {"equip_no": equip_no, **dict(equipment)},
                "requested_count": len(points),
                "success_count": len(successes),
                "failure_count": len(failures),
                "read_mode": "device_bundle_with_point_fallback",
                "waveform_mode": "latest",
                "requested_trend_hours": requested_trend_hours,
                "effective_trend_hours": effective_trend_hours,
                "trend_windows_used_hours": sorted(windows_used, reverse=True),
                "device_bundle_error": bundle_error,
                "items": successes,
                "failures": failures,
            },
        )

    async def point_rpm_batch(
        self, request: ToolCallRequest, data_access_token: str | None
    ) -> ToolCallResult:
        del data_access_token
        transient = current_graph_runtime().transient_tool_payloads
        batch = transient.get(f"latest:{PHM_WORKFLOW_POINT_DATA_BATCH_TOOL_ID}")
        batch = dict(batch) if isinstance(batch, Mapping) else {}
        items = [dict(item) for item in batch.get("items") or [] if isinstance(item, Mapping)]
        context = self._context(request)
        if not items:
            return ToolCallResult(
                tool_id=request.tool_id,
                status=ToolResultStatus.FAILED,
                error_code="DIAGNOSIS_POINT_DATA_BATCH_MISSING",
                error_message="逐测点数据批次为空，无法识别转速。",
            )

        async def extract(item: dict[str, Any]) -> dict[str, Any]:
            point = dict(item.get("point") or {})
            snapshot = dict(item.get("snapshot") or {})
            arguments, missing = build_phm_feature_arguments(
                tool_id=PHM_FEATURE_EXTRACT_RPM_TOOL_ID,
                state=_state_from_context(context, point),
                call_arguments={},
                transient_payloads={f"latest:{PHM_GET_DATA_SNAPSHOT_TOOL_ID}": snapshot},
            )
            if missing:
                return {
                    "ok": False,
                    "point": point,
                    "error": {"code": "RPM_WAVEFORM_MISSING", "message": "、".join(missing)},
                }
            payload: dict[str, Any] | None = None
            for attempt in range(self.feature_queue_retry_attempts + 1):
                try:
                    payload = await self.feature_client.call_tool(
                        "extract_rotational_speed_feature", arguments
                    )
                    break
                except Exception as exc:
                    error = _error_detail(exc)
                    if (
                        not _is_feature_queue_pressure(error)
                        or attempt >= self.feature_queue_retry_attempts
                    ):
                        return {"ok": False, "point": point, "error": error}
                    await asyncio.sleep(
                        self.feature_queue_retry_delay_seconds * float(attempt + 1)
                    )
            if payload is None:
                return {
                    "ok": False,
                    "point": point,
                    "error": {
                        "code": "PHM_FEATURE_MCP_TOOL_ERROR",
                        "message": "Feature MCP 未返回转速识别结果。",
                    },
                }
            return {
                "ok": True,
                "point": point,
                "speed_rpm": extract_supported_rpm(payload),
                "rpm_result": payload,
            }

        rows = await self._bounded(
            items,
            extract,
            max_parallel=min(self.max_parallel, self.feature_max_parallel),
        )
        return ToolCallResult(
            tool_id=request.tool_id,
            status=ToolResultStatus.SUCCESS,
            structured_content={
                "success": True,
                "requested_count": len(items),
                "completed_count": sum(1 for item in rows if item.get("ok")),
                "supported_count": sum(1 for item in rows if item.get("speed_rpm") is not None),
                "items": rows,
            },
        )

    async def point_diagnosis_batch(
        self, request: ToolCallRequest, data_access_token: str | None
    ) -> ToolCallResult:
        del data_access_token
        transient = current_graph_runtime().transient_tool_payloads
        data_batch = transient.get(f"latest:{PHM_WORKFLOW_POINT_DATA_BATCH_TOOL_ID}")
        rpm_batch = transient.get(f"latest:{PHM_WORKFLOW_POINT_RPM_BATCH_TOOL_ID}")
        data_batch = dict(data_batch) if isinstance(data_batch, Mapping) else {}
        rpm_batch = dict(rpm_batch) if isinstance(rpm_batch, Mapping) else {}
        data_items = [
            dict(item) for item in data_batch.get("items") or [] if isinstance(item, Mapping)
        ]
        rpm_by_point: dict[str, dict[str, Any]] = {}
        for row in rpm_batch.get("items") or []:
            if not isinstance(row, Mapping):
                continue
            point = row.get("point") if isinstance(row.get("point"), Mapping) else {}
            point_no = str(point.get("point_no") or point.get("wave_point_no") or "")
            if point_no:
                rpm_by_point[point_no] = dict(row)
        context = self._context(request)
        if not data_items:
            return ToolCallResult(
                tool_id=request.tool_id,
                status=ToolResultStatus.FAILED,
                error_code="DIAGNOSIS_POINT_DATA_BATCH_MISSING",
                error_message="逐测点数据批次为空，无法调用 Diagnosis MCP。",
            )

        async def diagnose(item: dict[str, Any]) -> dict[str, Any]:
            point = dict(item.get("point") or {})
            snapshot = dict(item.get("snapshot") or {})
            point_no = str(point.get("point_no") or point.get("wave_point_no") or "")
            rpm_row = rpm_by_point.get(point_no, {})
            transient_payloads: dict[str, Any] = {
                f"latest:{PHM_GET_DATA_SNAPSHOT_TOOL_ID}": snapshot,
            }
            rpm_result = rpm_row.get("rpm_result")
            if isinstance(rpm_result, Mapping):
                transient_payloads[f"latest:{PHM_FEATURE_EXTRACT_RPM_TOOL_ID}"] = dict(rpm_result)
            arguments, missing = build_phm_diagnosis_arguments(
                tool_id=PHM_DIAGNOSIS_POINT_TOOL_ID,
                state=_state_from_context(context, point),
                call_arguments={"use_model": True},
                transient_payloads=transient_payloads,
            )
            if missing:
                return {
                    "ok": False,
                    "point": point,
                    "speed_rpm": rpm_row.get("speed_rpm"),
                    "error": {
                        "code": "POINT_DIAGNOSIS_ARGUMENTS_MISSING",
                        "message": "、".join(missing),
                    },
                }
            try:
                payload = await self.diagnosis_client.call_tool("diagnose_point", arguments)
            except Exception as exc:
                return {
                    "ok": False,
                    "point": point,
                    "speed_rpm": rpm_row.get("speed_rpm"),
                    "error": _error_detail(exc),
                }
            return {
                "ok": True,
                "point": point,
                "speed_rpm": rpm_row.get("speed_rpm"),
                "diagnosis": payload,
            }

        rows = await self._bounded(data_items, diagnose)
        successes = [item for item in rows if item.get("ok")]
        failures = [item for item in rows if not item.get("ok")]
        if not successes:
            return ToolCallResult(
                tool_id=request.tool_id,
                status=ToolResultStatus.FAILED,
                error_code="DIAGNOSIS_ALL_POINTS_FAILED",
                error_message=f"Diagnosis MCP 未成功完成任何测点诊断；失败详情={failures}",
            )
        return ToolCallResult(
            tool_id=request.tool_id,
            status=ToolResultStatus.SUCCESS,
            structured_content={
                "success": True,
                "equipment": data_batch.get("equipment") or {},
                "success_count": len(successes),
                "failure_count": len(failures),
                "items": successes,
                "failures": failures,
                "upstream_data_failures": data_batch.get("failures") or [],
            },
        )

    async def rag_structure_status(
        self, request: ToolCallRequest, data_access_token: str | None
    ) -> ToolCallResult:
        del data_access_token
        return ToolCallResult(
            tool_id=request.tool_id,
            status=ToolResultStatus.SUCCESS,
            structured_content={
                "success": True,
                "connected": False,
                "status": "NOT_CONNECTED",
                "note": (
                    "设备结构知识 RAG 当前尚未接入。本轮仅使用 Asset MCP、健康度和报警的"
                    "可验证事实；主智能体不得补造内部结构、部件关系或维护知识。"
                ),
            },
        )


def public_workflow_payload(tool_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    source = dict(payload or {})

    def public_failure(item: Any) -> dict[str, Any]:
        row = dict(item) if isinstance(item, Mapping) else {}
        error = row.get("error") if isinstance(row.get("error"), Mapping) else {}
        return {
            "point": _public_point_identity(row.get("point") or {}),
            "error_code": error.get("code") or "POINT_EXECUTION_FAILED",
            "error_message": error.get("message"),
        }

    if tool_id == PHM_WORKFLOW_HEALTH_DIMENSIONS_TOOL_ID:
        record = (
            source.get("health_record") if isinstance(source.get("health_record"), Mapping) else {}
        )
        allowed = {"score", "total_score", "totalScore", "finalScore", "grade", "ts"}
        allowed.update(key for key, _, _ in _HEALTH_DIMENSIONS)
        return {
            "success": source.get("success"),
            "source": source.get("source"),
            "health_record": {key: value for key, value in record.items() if key in allowed},
            "deficits": source.get("deficits") or [],
            "deficit_count": source.get("deficit_count"),
        }
    if tool_id == PHM_WORKFLOW_HEALTH_COLLECTION_BATCH_TOOL_ID:
        return {
            "success": source.get("success"),
            "source": source.get("source"),
            "root": source.get("root") or {},
            "target_entity_level": source.get("target_entity_level"),
            "target_space_type": source.get("target_space_type"),
            "requested_count": source.get("requested_count"),
            "success_count": source.get("success_count"),
            "failure_count": source.get("failure_count"),
            "collection_truncated": source.get("collection_truncated"),
            "ranking_computed_in_code": False,
            "items": [
                {
                    "entity": dict(item.get("entity") or {}),
                    "health": {"data": {k:v for k,v in (((item.get("health") or {}).get("data") or {}) if isinstance((item.get("health") or {}).get("data"),dict) else {}).items() if k in {"score","total_score","finalScore","grade","ts","thresholdScore","trendScore","aiScore","mechanismScore"}}},
                    "duration_ms": item.get("duration_ms"),
                }
                for item in source.get("items") or []
                if isinstance(item, Mapping)
            ],
            "failures": [
                {
                    "entity": dict(item.get("entity") or {}),
                    "duration_ms": item.get("duration_ms"),
                    "error": dict(item.get("error") or {})
                    if isinstance(item.get("error"), Mapping)
                    else item.get("error"),
                }
                for item in source.get("failures") or []
                if isinstance(item, Mapping)
            ],
        }
    if tool_id == PHM_WORKFLOW_POINT_DATA_BATCH_TOOL_ID:
        return {
            "success": source.get("success"),
            "equipment": source.get("equipment") or {},
            "requested_count": source.get("requested_count"),
            "success_count": source.get("success_count"),
            "failure_count": source.get("failure_count"),
            "waveform_mode": source.get("waveform_mode"),
            "requested_trend_hours": source.get("requested_trend_hours"),
            "effective_trend_hours": source.get("effective_trend_hours"),
            "trend_windows_used_hours": source.get("trend_windows_used_hours") or [],
            "points": [
                _public_point_identity(item.get("point") or {})
                for item in source.get("items") or []
                if isinstance(item, Mapping)
            ]
            or [
                _public_point_identity(item)
                for item in source.get("points") or []
                if isinstance(item, Mapping)
            ],
            "failures": [public_failure(item) for item in source.get("failures") or []],
        }
    if tool_id == PHM_WORKFLOW_POINT_RPM_BATCH_TOOL_ID:
        return {
            "success": source.get("success"),
            "requested_count": source.get("requested_count"),
            "completed_count": source.get("completed_count"),
            "supported_count": source.get("supported_count"),
            "items": [
                {
                    "point": _public_point_identity(item.get("point") or {}),
                    "speed_rpm": item.get("speed_rpm"),
                    "supported": (item.get("rpm_result") or {}).get("supported")
                    if isinstance(item.get("rpm_result"), Mapping)
                    else False,
                    "error_code": (
                        item.get("error", {}).get("code")
                        if isinstance(item.get("error"), Mapping)
                        else None
                    ),
                    "error_message": (
                        item.get("error", {}).get("message")
                        if isinstance(item.get("error"), Mapping)
                        else None
                    ),
                }
                for item in source.get("items") or []
                if isinstance(item, Mapping)
            ],
        }
    if tool_id == PHM_WORKFLOW_POINT_DIAGNOSIS_BATCH_TOOL_ID:
        return {
            "success": source.get("success"),
            "equipment": source.get("equipment") or {},
            "success_count": source.get("success_count"),
            "failure_count": source.get("failure_count"),
            "items": [
                {
                    "point": _public_point_identity(item.get("point") or {}),
                    "speed_rpm": item.get("speed_rpm"),
                    "diagnosis": _compact_diagnosis_for_synthesis(
                        item.get("diagnosis") or {}
                    ),
                }
                for item in source.get("items") or []
                if isinstance(item, Mapping)
            ],
            "failures": [public_failure(item) for item in source.get("failures") or []],
            "upstream_data_failures": [
                public_failure(item) for item in source.get("upstream_data_failures") or []
            ],
        }
    return source


def format_workflow_result(tool_id: str, payload: Mapping[str, Any]) -> str:
    source = dict(payload or {})
    if tool_id == PHM_WORKFLOW_HEALTH_DIMENSIONS_TOOL_ID:
        deficits = source.get("deficits") or []
        if not deficits:
            return "PHM Data MCP 返回的四类健康度分项均为满分。"
        lines = ["健康度未满分的分项："]
        for item in deficits:
            if isinstance(item, Mapping):
                lines.append(
                    f"- {item.get('dimension')}：{item.get('score')}（距满分 {item.get('gap_to_full')}）"
                )
        return "\n".join(lines)
    if tool_id == PHM_WORKFLOW_HEALTH_ALARMS_TOOL_ID:
        return (
            f"已按扣分项查询报警类型：{', '.join(source.get('alarm_types') or []) or '无'}。\n"
            f"报警返回：{source.get('alarm_result') or {}}"
        )
    if tool_id == PHM_WORKFLOW_HEALTH_COLLECTION_BATCH_TOOL_ID:
        return (
            f"区域下属实体健康度并行读取完成：成功 {source.get('success_count')}/"
            f"{source.get('requested_count')}，失败 {source.get('failure_count')}。"
            "未在代码中计算最低/最高排序，完整事实交由主控模型统一判断。"
        )
    if tool_id == PHM_WORKFLOW_POINT_DATA_BATCH_TOOL_ID:
        return (
            f"逐测点数据读取完成：成功 {source.get('success_count')}/{source.get('requested_count')}，"
            f"失败 {source.get('failure_count')}；特征趋势请求窗口 "
            f"{source.get('requested_trend_hours')} 小时，实际窗口 "
            f"{source.get('effective_trend_hours')} 小时；波形使用最新一条。"
        )
    if tool_id == PHM_WORKFLOW_POINT_RPM_BATCH_TOOL_ID:
        return f"逐测点转速识别完成：执行 {source.get('completed_count')}/{source.get('requested_count')}，可靠转速 {source.get('supported_count')} 个。"
    if tool_id == PHM_WORKFLOW_POINT_DIAGNOSIS_BATCH_TOOL_ID:
        return f"逐测点 Diagnosis MCP 调用完成：成功 {source.get('success_count')}，失败 {source.get('failure_count')}。"
    if tool_id == PHM_WORKFLOW_RAG_STRUCTURE_STATUS_TOOL_ID:
        return str(source.get("note") or "设备结构知识 RAG 当前尚未接入。")
    return str(source)


def phm_workflow_tool_descriptors(settings: Settings) -> list[ToolDescriptor]:
    common_schema = {
        "type": "object",
        "properties": {
            "required_entity_level": {"type": "string"},
            "_workflow_context": {"type": "object"},
        },
        "additionalProperties": True,
    }

    def descriptor(
        tool_id: str,
        name: str,
        description: str,
        *,
        enabled: bool,
        timeout: float,
        contains_binary: bool = False,
    ) -> ToolDescriptor:
        return ToolDescriptor(
            tool_id=tool_id,
            display_name=name,
            provider_type=ToolProviderType.LOCAL,
            description=description,
            input_schema=common_schema,
            output_schema={"type": "object", "additionalProperties": True},
            enabled=enabled,
            timeout_seconds=timeout,
            metadata={
                "workflow_internal": True,
                "contains_binary_payload": contains_binary,
                "persist_full_output": not contains_binary,
            },
        )

    data_enabled = bool(settings.phm_data_mcp_enabled and settings.phm_data_mcp_url)
    feature_enabled = bool(settings.phm_feature_mcp_enabled and settings.phm_feature_mcp_url)
    diagnosis_enabled = bool(settings.phm_diagnosis_mcp_enabled and settings.phm_diagnosis_mcp_url)
    return [
        descriptor(
            PHM_WORKFLOW_HEALTH_DIMENSIONS_TOOL_ID,
            "健康度扣分项识别",
            "从 PHM Data MCP 刷新同一设备的结构化健康度结果，校验四类分项完整后识别全部扣分项。",
            enabled=data_enabled,
            timeout=settings.phm_data_mcp_timeout_seconds,
        ),
        descriptor(
            PHM_WORKFLOW_HEALTH_ALARMS_TOOL_ID,
            "健康度扣分项报警检索",
            "只查询未满分维度对应的阈值、趋势、AI、机理/诊断报警。",
            enabled=data_enabled,
            timeout=settings.phm_data_mcp_timeout_seconds,
        ),
        descriptor(
            PHM_WORKFLOW_HEALTH_COLLECTION_BATCH_TOOL_ID,
            "区域下属实体健康度并行读取",
            "对 Asset MCP 返回的真实空间/设备集合限流并行调用 Data MCP 健康度；不在代码中按文本规则筛选或排名。",
            enabled=data_enabled,
            timeout=max(900.0, settings.phm_data_mcp_timeout_seconds),
        ),
        descriptor(
            PHM_WORKFLOW_POINT_DATA_BATCH_TOOL_ID,
            "逐测点数据并行读取",
            "对 Asset MCP 返回的全部振动加速度测点限流并行读取 Data MCP 快照。",
            enabled=data_enabled,
            timeout=max(900.0, settings.phm_data_mcp_timeout_seconds),
            contains_binary=True,
        ),
        descriptor(
            PHM_WORKFLOW_POINT_RPM_BATCH_TOOL_ID,
            "逐测点转速并行识别",
            "对每个成功波形限流并行调用 Feature MCP 转速识别。",
            enabled=feature_enabled,
            timeout=max(900.0, settings.phm_feature_mcp_rpm_timeout_seconds),
            contains_binary=True,
        ),
        descriptor(
            PHM_WORKFLOW_POINT_DIAGNOSIS_BATCH_TOOL_ID,
            "逐测点诊断并行执行",
            "把每个测点的数据与同测点转速传给 Diagnosis MCP，再交由主智能体汇总。",
            enabled=diagnosis_enabled,
            timeout=max(1800.0, settings.phm_diagnosis_mcp_timeout_seconds),
            contains_binary=True,
        ),
        descriptor(
            PHM_WORKFLOW_RAG_STRUCTURE_STATUS_TOOL_ID,
            "设备结构资料接入状态",
            "显式标记设备结构知识 RAG 尚未接入，防止主智能体补造结构知识。",
            enabled=True,
            timeout=5.0,
        ),
    ]
