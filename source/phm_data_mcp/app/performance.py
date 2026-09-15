from __future__ import annotations

import contextvars
import inspect
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Callable

_current_recorder: contextvars.ContextVar["PerformanceRecorder | None"] = contextvars.ContextVar(
    "phm_performance_recorder", default=None
)
_current_parent: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "phm_performance_parent", default=None
)

_SECRET_MARKERS = ("password", "secret", "api_key", "apikey", "authorization", "cookie", "token")
_LARGE_MARKERS = ("values_base64", "payload_base64", "float32_base64", "byte_values", "samples")


def safe_value(value: Any, key: str = "", *, max_chars: int = 300) -> Any:
    lowered = str(key).lower()
    if any(marker in lowered for marker in _SECRET_MARKERS):
        return "[REDACTED]"
    if any(marker == lowered for marker in _LARGE_MARKERS):
        try:
            return f"[OMITTED length={len(value)}]"
        except Exception:
            return "[OMITTED]"
    if isinstance(value, dict):
        return {str(k): safe_value(v, str(k), max_chars=max_chars) for k, v in list(value.items())[:40]}
    if isinstance(value, (list, tuple)):
        items = list(value)
        rendered = [safe_value(v, key, max_chars=max_chars) for v in items[:30]]
        if len(items) > 30:
            rendered.append(f"[TRUNCATED total={len(items)}]")
        return rendered
    if isinstance(value, str) and len(value) > max_chars:
        return value[:max_chars] + f"...[TRUNCATED chars={len(value)}]"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)[:max_chars]


@dataclass
class PerformanceRecorder:
    enabled: bool
    service: str
    tool_name: str
    max_spans: int = 120
    spans: list[dict[str, Any]] = field(default_factory=list)
    started_ns: int = field(default_factory=time.perf_counter_ns)
    _counter: int = 0

    @contextmanager
    def span(
        self,
        code: str,
        name: str,
        *,
        category: str = "internal",
        description: str = "",
        component: str | None = None,
        metrics: dict[str, Any] | None = None,
    ):
        if not self.enabled or len(self.spans) >= self.max_spans:
            yield None
            return
        self._counter += 1
        span_id = f"mcp-{self._counter:03d}"
        parent = _current_parent.get()
        token = _current_parent.set(span_id)
        start_ns = time.perf_counter_ns()
        row = {
            "span_id": span_id,
            "parent_span_id": parent,
            "code": code,
            "name": name,
            "category": category,
            "description": description,
            "component": component or self.service,
            "status": "RUNNING",
            "started_offset_ms": round((start_ns - self.started_ns) / 1_000_000, 3),
            "duration_ms": None,
            "metrics": safe_value(metrics or {}),
        }
        self.spans.append(row)
        try:
            yield row
        except Exception as exc:
            row["status"] = "FAILED"
            row["error"] = f"{type(exc).__name__}: {exc}"[:600]
            raise
        else:
            row["status"] = "COMPLETED"
        finally:
            row["duration_ms"] = round((time.perf_counter_ns() - start_ns) / 1_000_000, 3)
            _current_parent.reset(token)

    def export(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "service": self.service,
            "tool_name": self.tool_name,
            "enabled": self.enabled,
            "total_duration_ms": round((time.perf_counter_ns() - self.started_ns) / 1_000_000, 3),
            "span_count": len(self.spans),
            "spans": self.spans[: self.max_spans],
        }


def current_recorder() -> PerformanceRecorder | None:
    return _current_recorder.get()


@contextmanager
def performance_request(settings: Any, service: str, tool_name: str):
    enabled = bool(getattr(settings, "performance_monitor_enabled", False))
    max_spans = int(getattr(settings, "performance_trace_max_spans", 120) or 120)
    recorder = PerformanceRecorder(enabled=enabled, service=service, tool_name=tool_name, max_spans=max_spans)
    token_rec = _current_recorder.set(recorder if enabled else None)
    token_parent = _current_parent.set(None)
    try:
        with recorder.span(
            f"{service}.tool",
            f"执行 {tool_name}",
            category="mcp_tool",
            description=f"{service} 接收并执行 MCP 工具 {tool_name}",
        ):
            yield recorder
    finally:
        _current_parent.reset(token_parent)
        _current_recorder.reset(token_rec)


def attach_trace(result: Any, recorder: PerformanceRecorder) -> Any:
    if not recorder.enabled:
        return result
    trace = recorder.export()
    if isinstance(result, dict):
        result = dict(result)
        result["_performance_trace"] = trace
        return result
    if hasattr(result, "model_dump"):
        payload = result.model_dump(mode="json")
        payload["_performance_trace"] = trace
        return payload
    return {"data": result, "_performance_trace": trace}


def _generic_name(component_cn: str, method_name: str) -> str:
    labels = {
        "query": "执行查询",
        "ping": "检查数据库连接",
        "get_waveform": "查询振动波形",
        "get_feature_trends": "查询特征趋势",
        "get_feature_trends_bulk": "批量查询特征趋势",
        "get_temperature_trends": "查询温度趋势",
        "availability": "检查数据可用性",
        "get_device_health": "查询设备健康度",
        "get_space_health": "查询区域健康度",
        "embed": "生成语义向量",
        "rerank": "候选结果精排",
        "understand": "解析资产查询语义",
        "resolve": "执行实体解析",
        "diagnose": "调用诊断大模型",
        "analyze": "执行图谱分析",
        "extract_features": "提取振动特征",
        "extract_rotational_speed_feature": "提取转速特征",
        "run": "执行算法",
        "generate": "调用大模型生成 SQL",
    }
    return f"{component_cn}：{labels.get(method_name, method_name)}"


def instrument_object(
    obj: Any,
    *,
    component_code: str,
    component_cn: str,
    category: str,
    include: set[str] | None = None,
    exclude: set[str] | None = None,
) -> Any:
    exclude = set(exclude or set()) | {"close", "start", "stop", "configured", "enabled"}
    names = include or {
        name for name in dir(obj)
        if not name.startswith("_") and name not in exclude
    }
    for name in sorted(names):
        if name in exclude:
            continue
        try:
            original = getattr(obj, name)
        except Exception:
            continue
        if not callable(original) or getattr(original, "__phm_perf_wrapped__", False):
            continue
        if inspect.iscoroutinefunction(original):
            @wraps(original)
            async def async_wrapped(*args, __orig=original, __name=name, **kwargs):
                rec = current_recorder()
                if rec is None:
                    return await __orig(*args, **kwargs)
                with rec.span(
                    f"{component_code}.{__name}",
                    _generic_name(component_cn, __name),
                    category=category,
                    description=f"在 {component_cn} 中执行 {__name}",
                ):
                    return await __orig(*args, **kwargs)
            async_wrapped.__phm_perf_wrapped__ = True
            try:
                setattr(obj, name, async_wrapped)
            except Exception:
                pass
        else:
            @wraps(original)
            def sync_wrapped(*args, __orig=original, __name=name, **kwargs):
                rec = current_recorder()
                if rec is None:
                    return __orig(*args, **kwargs)
                with rec.span(
                    f"{component_code}.{__name}",
                    _generic_name(component_cn, __name),
                    category=category,
                    description=f"在 {component_cn} 中执行 {__name}",
                ):
                    return __orig(*args, **kwargs)
            sync_wrapped.__phm_perf_wrapped__ = True
            try:
                setattr(obj, name, sync_wrapped)
            except Exception:
                pass
    return obj
