from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from app.orchestration.entity_lifecycle import can_down_drill_equipment_to_point, entity_anchor
from app.tools.phm_data_mcp import (
    PHM_CHECK_DATA_AVAILABILITY_TOOL_ID,
    PHM_GET_DATA_SNAPSHOT_TOOL_ID,
    PHM_GET_FEATURE_TREND_TOOL_ID,
    PHM_GET_TEMPERATURE_TREND_TOOL_ID,
    PHM_GET_WAVEFORM_TOOL_ID,
)


@dataclass(frozen=True, slots=True)
class EntityCapabilityRequirement:
    """A deterministic data requirement derived from the selected PHM Data tool.

    This layer deliberately does *not* classify the user's intent and does not read
    business data. It only answers one orchestration question: when a tool needs a
    point-scoped identity but the conversation currently anchors an equipment entity,
    which type of point must Asset MCP resolve inside that equipment?
    """

    capability: str
    required_entity_level: str
    point_type: str
    objective: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_REQUIREMENTS: dict[str, EntityCapabilityRequirement] = {
    PHM_GET_TEMPERATURE_TREND_TOOL_ID: EntityCapabilityRequirement(
        capability="temperature_trend",
        required_entity_level="point",
        point_type="temperature",
        objective="在当前已锚定设备内部查询温度测点，以读取温度历史趋势",
    ),
    PHM_GET_WAVEFORM_TOOL_ID: EntityCapabilityRequirement(
        capability="vibration_waveform",
        required_entity_level="point",
        point_type="vibration_acceleration",
        objective="在当前已锚定设备内部查询振动加速度测点，以读取振动波形",
    ),
    PHM_GET_FEATURE_TREND_TOOL_ID: EntityCapabilityRequirement(
        capability="vibration_feature_trend",
        required_entity_level="point",
        point_type="vibration_acceleration",
        objective="在当前已锚定设备内部查询振动加速度测点，以读取振动特征趋势",
    ),
    PHM_GET_DATA_SNAPSHOT_TOOL_ID: EntityCapabilityRequirement(
        capability="diagnosis_data_snapshot",
        required_entity_level="point",
        point_type="vibration_acceleration",
        objective="在当前已锚定设备内部查询振动加速度测点，以准备诊断数据快照",
    ),
    PHM_CHECK_DATA_AVAILABILITY_TOOL_ID: EntityCapabilityRequirement(
        capability="data_availability",
        required_entity_level="point",
        point_type="vibration_acceleration",
        objective="在当前已锚定设备内部查询振动加速度测点，以检查数据可用性",
    ),
}


def capability_requirement_for_tool(tool_id: str) -> EntityCapabilityRequirement | None:
    return _REQUIREMENTS.get(str(tool_id or ""))


def should_expand_equipment_capability(
    state: Mapping[str, Any],
    tool_id: str,
) -> bool:
    """Whether the selected tool should resolve a point inside the current equipment.

    Entity lifecycle remains authoritative. A fresh/new entity (replace/none) is not
    silently inherited here; fuzzy resolution is still allowed for that case. This
    avoids the opposite bug where an old device is reused after the user names another
    device.
    """

    requirement = capability_requirement_for_tool(tool_id)
    if requirement is None:
        return False
    return can_down_drill_equipment_to_point(state)


def current_capability_context(state: Mapping[str, Any]) -> dict[str, Any]:
    """Return a compact, auditable snapshot of the current equipment capability scope."""

    anchor = entity_anchor(state)
    return {
        "entity_type": anchor.get("entity_type") or anchor.get("match_type"),
        "equip_no": anchor.get("equip_no") or anchor.get("device_code"),
        "equip_name": anchor.get("equip_name") or anchor.get("equipment_name"),
    }
