from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LayeredError(BaseModel):
    """Stable two-audience error returned by every workflow/agent/tool step."""

    code: str
    public_message: str
    operator_message: str
    component: str
    workflow_id: str | None = None
    step_id: str | None = None
    request_id: str | None = None
    retryable: bool = False
    upstream_failures: list[dict[str, Any]] = Field(default_factory=list)


def public_error_payload(value: Any) -> Any:
    """Recursively remove operator-only fields from model-visible payloads."""

    if isinstance(value, list):
        return [public_error_payload(item) for item in value]
    if not isinstance(value, dict):
        return value
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"operator_error", "operator_message", "upstream_failures"}:
            continue
        if key == "error" and isinstance(item, dict):
            cleaned[key] = {
                name: public_error_payload(raw)
                for name, raw in item.items()
                if name in {"code", "public_message", "component", "retryable"}
            }
        else:
            cleaned[key] = public_error_payload(item)
    return cleaned


def default_public_message(code: str, component: str) -> str:
    normalized = str(code or "INTERNAL_ERROR").upper()
    label = component or "相关能力"
    if "TIMEOUT" in normalized:
        return f"{label}响应超时，本次结果未能完整生成，请稍后重试。"
    if any(token in normalized for token in ("UNAVAILABLE", "CIRCUIT", "CONNECTION")):
        return f"{label}暂时不可用，本次查询未能完成，请稍后重试。"
    if any(token in normalized for token in ("ENTITY_REQUIRED", "ARGUMENTS_MISSING")):
        return "当前问题缺少可确认的区域、设备或测点信息，请补充更完整的名称或编码。"
    if "NOT_FOUND" in normalized:
        return "没有找到满足当前条件的业务数据，请核对查询对象或条件。"
    if "DEPENDENCY" in normalized:
        return "本次业务链路的前置步骤未成功，后续分析已安全停止。"
    return f"{label}未能完成本次处理，请稍后重试或联系管理员。"


def build_layered_error(
    *,
    code: str,
    operator_message: str,
    component: str,
    public_message: str | None = None,
    workflow_id: str | None = None,
    step_id: str | None = None,
    request_id: str | None = None,
    retryable: bool = False,
    upstream_failures: list[dict[str, Any]] | None = None,
) -> LayeredError:
    return LayeredError(
        code=code,
        public_message=public_message or default_public_message(code, component),
        operator_message=operator_message or code,
        component=component,
        workflow_id=workflow_id,
        step_id=step_id,
        request_id=request_id,
        retryable=retryable,
        upstream_failures=list(upstream_failures or []),
    )
